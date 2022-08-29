'''
Evaluate the decoder at specified values of z
'''
import numpy as np
import os
import argparse
from datetime import datetime as dt
import pprint

import torch
from torch.cuda.amp import autocast

from tomodrgn import mrc
from tomodrgn import utils
from tomodrgn.models import TiltSeriesHetOnlyVAE

log = utils.log
vlog = utils.vlog

def add_args(parser):
    parser.add_argument('weights', help='Model weights.pkl from train_vae')
    parser.add_argument('-c', '--config', required=True, help='config.pkl file from train_vae')
    parser.add_argument('-o', type=os.path.abspath, required=True, help='Output .mrc or directory')
    parser.add_argument('--prefix', default='vol_', help='Prefix when writing out multiple .mrc files')
    parser.add_argument('--no-amp', action='store_true', help='Disable use of mixed-precision training')
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

def main(args):
    check_z_inputs(args)
    t1 = dt.now()

    ## set the device
    use_cuda = torch.cuda.is_available()
    log('Use cuda {}'.format(use_cuda))
    if use_cuda:
        torch.set_default_tensor_type(torch.cuda.FloatTensor)
    else:
        log('WARNING: No GPUs detected')

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
    model.eval()

    use_amp = not args.no_amp
    with torch.no_grad():
        with autocast(enabled=use_amp):

            ### Multiple z ###
            if args.z_start or args.zfile:

                ### Get z values
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

                if not os.path.exists(args.o):
                    os.makedirs(args.o)

                log(f'Generating {len(z)} volumes')
                for i,zz in enumerate(z):
                    log(zz)
                    if args.downsample:
                        extent = lattice.extent * (args.downsample/(D-1))
                        vol = model.decoder.eval_volume(lattice.get_downsample_coords(args.downsample+1),
                                                        args.downsample+1, extent, norm, zz)
                    else:
                        vol = model.decoder.eval_volume(lattice.coords, lattice.D, lattice.extent, norm, zz)
                    out_mrc = f'{args.o}/{args.prefix}{i:03d}.mrc'
                    if args.flip:
                        vol = vol[::-1]
                    if args.invert:
                        vol *= -1
                    mrc.write(out_mrc, vol.astype(np.float32), Apix=args.Apix)

            ### Single z ###
            else:
                z = np.array(args.z)
                log(z)
                if args.downsample:
                    extent = lattice.extent * (args.downsample/(D-1))
                    vol = model.decoder.eval_volume(lattice.get_downsample_coords(args.downsample+1),
                                                    args.downsample+1, extent, norm, z)
                else:
                    vol = model.decoder.eval_volume(lattice.coords, lattice.D, lattice.extent, norm, z)
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

