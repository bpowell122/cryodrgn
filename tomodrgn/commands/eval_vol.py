'''
Evaluate the decoder at specified values of z
'''
import numpy as np
import os
import argparse
from datetime import datetime as dt
import pprint
import multiprocessing as mp

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
from torch.cuda.amp import autocast

from tomodrgn import mrc, utils
from tomodrgn.models import TiltSeriesHetOnlyVAE

log = utils.log
vlog = utils.vlog

def add_args(parser):
    parser.add_argument('-w', '--weights', help='Model weights from train_vae')
    parser.add_argument('-c', '--config', required=True, help='config.pkl file from train_vae')
    parser.add_argument('-o', type=os.path.abspath, required=True, help='Output .mrc or directory')
    parser.add_argument('--prefix', default='vol_', help='Prefix when writing out multiple .mrc files')
    parser.add_argument('--no-amp', action='store_true', help='Disable use of mixed-precision training')
    parser.add_argument('-b', '--batch-size', type=int, default=64, help='Batch size to parallelize volume generation')
    parser.add_argument('--multigpu', action='store_true', help='Parallelize training across all detected GPUs. Specify GPUs i,j via `export CUDA_VISIBLE_DEVICES=i,j`')
    parser.add_argument('-v', '--verbose', action='store_true', help='Increases verbosity')

    group = parser.add_argument_group('Specify z values')
    group.add_argument('-z', type=np.float32, nargs='*', help='Specify one z-value')
    group.add_argument('--z-start', type=np.float32, nargs='*', help='Specify a starting z-value')
    group.add_argument('--z-end', type=np.float32, nargs='*', help='Specify an ending z-value')
    group.add_argument('-n', type=int, default=10, help='Number of structures between [z_start, z_end]')
    group.add_argument('--zfile', help='Text/.pkl file with z-values to evaluate')

    group = parser.add_argument_group('Volume arguments')
    group.add_argument('--Apix', type=float, default=1, help='Pixel size to add to output .mrc header')
    group.add_argument('--flip', action='store_true', help='Flip handedness of output volume')
    group.add_argument('--invert', action='store_true', help='Invert contrast of output volume')
    group.add_argument('-d','--downsample', type=int, help='Downsample volumes to this box size (pixels)')

    return parser


def check_z_inputs(args):
    if args.z_start:
        assert args.z_end, "Must provide --z-end with argument --z-start"
    assert sum((bool(args.z), bool(args.z_start), bool(args.zfile))) == 1, \
        "Must specify either -z OR --z-start/--z-end OR --zfile"


class ZDataset(Dataset):
    def __init__(self, z):
        self.z = z
        self.N = z.shape[0]

    def __len__(self):
        return self.N

    def __getitem__(self, index):
        return self.z[index]


class DummyModel(nn.Module):
    '''
    wrapper for nn.DataParallel to split data across batch axis and run eval_volume_batch on all GPUs
    '''
    def __init__(self, model):
        super(DummyModel, self).__init__()
        self.model = model

    def forward(self, *args, **kwargs):
        return self.model.decoder.eval_volume_batch(*args, **kwargs)


def main(args):
    check_z_inputs(args)
    t1 = dt.now()

    ## set the device
    device = utils.get_default_device()
    torch.set_grad_enabled(False)

    log(args)
    cfg = utils.load_pkl(args.config)
    log('Loaded configuration:')
    pprint.pprint(cfg)

    D = cfg['lattice_args']['D'] # image size + 1
    zdim = cfg['model_args']['zdim']
    norm = cfg['dataset_args']['norm']

    if args.downsample:
        assert args.downsample % 2 == 0, "Boxsize must be even"
        assert args.downsample <= D - 1, "Must be smaller than original box size"
    
    model, lattice = TiltSeriesHetOnlyVAE.load(cfg, args.weights)
    model = DummyModel(model).to(device)
    model.eval()

    use_amp = not args.no_amp
    with autocast(enabled=use_amp):

        if args.downsample:
            fft_boxsize = args.downsample + 1
            coords = lattice.get_downsample_coords(fft_boxsize)
            extent = lattice.extent * (args.downsample / (D - 1))
        else:
            fft_boxsize = lattice.D
            coords = lattice.coords
            extent = lattice.extent

        ### Multiple z ###
        if args.z_start or args.zfile:

            # Get z values
            if args.z_start:
                args.z_start = np.array(args.z_start)
                args.z_end = np.array(args.z_end)
                z = np.repeat(np.arange(args.n,dtype=np.float32), zdim).reshape((args.n, zdim))
                z *= ((args.z_end - args.z_start)/(args.n-1))
                z += args.z_start
            else:
                if args.zfile.endswith('.pkl'):
                    z = utils.load_pkl(args.zfile)
                else:
                    z = np.loadtxt(args.zfile).reshape(-1, zdim)
            assert z.shape[1] == zdim

            # parallelize
            if args.multigpu and torch.cuda.device_count() > 1:
                log(f'Using {torch.cuda.device_count()} GPUs!')
                args.batch_size *= torch.cuda.device_count()
                log(f'Increasing batch size to {args.batch_size}')
                model = nn.DataParallel(model)
            elif args.multigpu:
                log(f'WARNING: --multigpu selected, but {torch.cuda.device_count()} GPUs detected')

            # Make output directory
            if not os.path.exists(args.o):
                os.makedirs(args.o)

            # preallocate concatenated coords, z, and keep(mask)
            coords_zz = torch.zeros((args.batch_size, fft_boxsize, fft_boxsize**2, 3+zdim), dtype=coords.dtype)  # B x D(z) x D**2(xy) x 3+zdim
            for i, dz in enumerate(torch.linspace(-extent, extent, steps=fft_boxsize)):
                coords_zz[:, i, :, :3] = (coords + torch.tensor([0, 0, dz]).view(1, 1, -1, 3))
            keep = (coords_zz[0, :, :, :3].pow(2).sum(dim=-1) <= extent ** 2).view(fft_boxsize, -1)
            keep = keep.expand(torch.cuda.device_count(), *keep.shape)

            # prepare threadpool for parallelized file writing
            pool = mp.pool.ThreadPool()

            # send tensors to GPU
            coords_zz = coords_zz.to(device)
            keep = keep.to(device)
            norm = torch.tensor(norm)
            norm = norm.expand(torch.cuda.device_count(), *norm.shape).to(device)

            # construct dataset and dataloader
            z = ZDataset(z)
            z_iterator = DataLoader(z, batch_size=args.batch_size, shuffle=False)
            log(f'Generating {len(z)} volumes in batches of {args.batch_size}')
            for i, zz in enumerate(z_iterator):
                log(f'    Generating volume batch {i}')
                if args.verbose:
                    log(zz)
                coords_zz[:len(zz), :, :, 3:] = zz.unsqueeze(1).unsqueeze(1)
                vols_batch = model(coords_zz, keep, norm)
                vols_batch = vols_batch.cpu().numpy()
                if args.flip:
                    vols_batch = vols_batch[:, ::-1]
                if args.invert:
                    vols_batch *= -1
                out_mrcs = [f'{args.o}/{args.prefix}{i*args.batch_size+j:03d}.mrc' for j in range(len(zz))]
                pool.starmap_async(mrc.write, zip(out_mrcs, vols_batch[:len(out_mrcs)]), chunksize=4)

        ### Single z ###
        else:
            z = np.array(args.z)
            log(z)
            vol = model.decoder.eval_volume(coords, fft_boxsize, extent, norm, z)
            if args.flip:
                vol = vol[::-1]
            if args.invert:
                vol *= -1
            mrc.write(args.o, vol.astype(np.float32), Apix=args.Apix)

    td = dt.now()-t1
    log(f'Finished in {td}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    args = add_args(parser).parse_args()
    utils._verbose = args.verbose
    main(args)

