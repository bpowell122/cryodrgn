'''
Train a VAE for heterogeneous reconstruction with known pose for tomography data
'''
import numpy as np
import sys, os
import argparse
import pickle
from datetime import datetime as dt

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.cuda.amp import GradScaler, autocast

import tomodrgn
from tomodrgn import utils
from tomodrgn import dataset
from tomodrgn import ctf
from tomodrgn import dose

from tomodrgn.models import TiltSeriesHetOnlyVAE
from tomodrgn.lattice import Lattice
from tomodrgn.beta_schedule import get_beta_schedule

log = utils.log
vlog = utils.vlog
# TODO fix logging: everything to flog instead of log, and optional vlog where appropriate

def add_args(parser):
    parser.add_argument('particles', type=os.path.abspath, help='Input particles (.mrcs, .star, .cs, or .txt)')
    parser.add_argument('-o', '--outdir', type=os.path.abspath, required=True, help='Output directory to save model')
    parser.add_argument('--zdim', type=int, required=True, help='Dimension of latent variable')
    parser.add_argument('--load', metavar='WEIGHTS.PKL', help='Initialize training from a checkpoint')
    parser.add_argument('--checkpoint', type=int, default=1, help='Checkpointing interval in N_EPOCHS (default: %(default)s)')
    parser.add_argument('--log-interval', type=int, default=200, help='Logging interval in N_PTCLS (default: %(default)s)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Increaes verbosity')
    parser.add_argument('--seed', type=int, default=np.random.randint(0,100000), help='Random seed')

    group = parser.add_argument_group('Dataset loading')
    group.add_argument('--ind', type=os.path.abspath, metavar='PKL', help='Filter particle stack by these indices')
    group.add_argument('--uninvert-data', dest='invert_data', action='store_false', help='Do not invert data sign')
    group.add_argument('--no-window', dest='window', action='store_false', help='Turn off real space windowing of dataset')
    group.add_argument('--window-r', type=float, default=.85,  help='Windowing radius')
    group.add_argument('--datadir', type=os.path.abspath, help='Path prefix to particle stack if loading relative paths from a .star or .cs file')
    group.add_argument('--lazy', action='store_true', help='Lazy loading if full dataset is too large to fit in memory (Should copy dataset to SSD)')
    group.add_argument('--Apix', type=float, default=1.0, help='Override A/px from input starfile; useful if starfile does not have _rlnDetectorPixelSize col')

    group.add_argument_group('Tilt series')
    group.add_argument('--recon-tilt-weight', action='store_true', help='Weight reconstruction loss by cosine(tilt_angle)')
    group.add_argument('--recon-dose-weight', action='store_true', help='Weight reconstruction loss per tilt per pixel by dose dependent amplitude attenuation')
    group.add_argument('--dose-override', type=float, default=None, help='Manually specify dose in e- / A2 / tilt')
    group.add_argument('--l-dose-mask', action='store_true', help='Do not train on frequencies exposed to > 2.5x critical dose. Training lattice is intersection of this with --l-extent')
    group.add_argument('--sample-ntilts', type=int, default=None, help='Number of tilts to sample from each particle per epoch. Default: min(ntilts) from dataset')

    group = parser.add_argument_group('Training parameters')
    group.add_argument('-n', '--num-epochs', type=int, default=20, help='Number of training epochs')
    group.add_argument('-b','--batch-size', type=int, default=8, help='Minibatch size')
    group.add_argument('--wd', type=float, default=0, help='Weight decay in Adam optimizer')
    group.add_argument('--lr', type=float, default=1e-4, help='Learning rate in Adam optimizer')
    group.add_argument('--beta', default=None, help='Choice of beta schedule or a constant for KLD weight')
    group.add_argument('--beta-control', type=float, help='KL-Controlled VAE gamma. Beta is KL target')
    group.add_argument('--norm', type=float, nargs=2, default=None, help='Data normalization as shift, 1/scale (default: 0, std of dataset)')
    group.add_argument('--no-amp', action='store_false', help='Disable use of mixed-precision training')
    group.add_argument('--multigpu', action='store_true', help='Parallelize training across all detected GPUs')

    group = parser.add_argument_group('Encoder Network')
    group.add_argument('--enc-layers-A', dest='qlayersA', type=int, default=3, help='Number of hidden layers for each tilt')
    group.add_argument('--enc-dim-A', dest='qdimA', type=int, default=256, help='Number of nodes in hidden layers for each tilt')
    group.add_argument('--out-dim-A', type=int, default=128, help='Number of nodes in output layer of encA == ntilts * number of nodes input to encB')
    group.add_argument('--enc-layers-B', dest='qlayersB', type=int, default=1, help='Number of hidden layers encoding merged tilts')
    group.add_argument('--enc-dim-B', dest='qdimB', type=int, default=256, help='Number of nodes in hidden layers encoding merged tilts')
    group.add_argument('--enc-mask', type=int, help='Circular mask of image for encoder (default: D/2; -1 for no mask)')

    group = parser.add_argument_group('Decoder Network')
    group.add_argument('--dec-layers', dest='players', type=int, default=3, help='Number of hidden layers')
    group.add_argument('--dec-dim', dest='pdim', type=int, default=256, help='Number of nodes in hidden layers')
    group.add_argument('--pe-type', choices=('geom_ft','geom_full','geom_lowf','geom_nohighf','linear_lowf', 'gaussian', 'none'), default='geom_lowf', help='Type of positional encoding')
    group.add_argument('--feat-sigma', type=float, default=0.5, help="Scale for random Gaussian features")
    group.add_argument('--pe-dim', type=int, help='Num features in positional encoding')
    group.add_argument('--activation', choices=('relu','leaky_relu'), default='relu', help='Activation')
    group.add_argument('--use-decoder-symmetry', action='store_true', help='Exploit fourier symmetry to only decode half of each image. Reduces GPU memory usage at cost of longer epoch times')
    return parser


def get_latest(args):
    # assumes args.num_epochs > latest checkpoint
    log('Detecting latest checkpoint...')
    weights = [f'{args.outdir}/weights.{i}.pkl' for i in range(args.num_epochs)]
    weights = [f for f in weights if os.path.exists(f)]
    args.load = weights[-1]
    log(f'Loading {args.load}')
    return args


def save_config(args, dataset, lattice, model, out_config):
    dataset_args = dict(particles=args.particles,
                        norm=dataset.norm,
                        ntilts=dataset.ntilts_training,
                        invert_data=args.invert_data,
                        ind=args.ind,
                        window=args.window,
                        window_r=args.window_r,
                        datadir=args.datadir)
    lattice_args = dict(D=lattice.D,
                        extent=lattice.extent,
                        ignore_DC=lattice.ignore_DC)
    model_args = dict(in_dim=model.in_dim,
                      qlayersA=args.qlayersA,
                      qdimA=args.qdimA,
                      qlayersB=args.qlayersB,
                      qdimB=args.qdimB,
                      out_dimA=args.out_dim_A,
                      players=args.players,
                      pdim=args.pdim,
                      zdim=args.zdim,
                      enc_mask=args.enc_mask,
                      pe_type=args.pe_type,
                      feat_sigma=args.feat_sigma,
                      pe_dim=args.pe_dim,
                      domain='fourier',
                      activation=args.activation,
                      l_dose_mask=args.l_dose_mask,
                      use_decoder_symmetry = args.use_decoder_symmetry)
    training_args = dict(n=args.num_epochs,
                         B=args.batch_size,
                         wd=args.wd,
                         lr=args.lr,
                         beta=args.beta,
                         beta_control=args.beta_control,
                         amp=not args.no_amp,
                         multigpu=args.multigpu,
                         lazy=args.lazy,
                         recon_dose_weight=args.recon_dose_weight,
                         dose_override=args.dose_override,
                         recon_tilt_weight=args.recon_tilt_weight,
                         verbose=args.verbose,
                         log_interval=args.log_interval,
                         checkpoint=args.checkpoint,
                         outdir=args.outdir)
    config = dict(dataset_args=dataset_args,
                  lattice_args=lattice_args,
                  model_args=model_args,
                  training_args=training_args)
    config['seed'] = args.seed
    with open(out_config, 'wb') as f:
        pickle.dump(config, f)
        meta = dict(time=dt.now(),
                    cmd=sys.argv,
                    version=tomodrgn.__version__)
        pickle.dump(meta, f)

def train_batch(scaler, model, lattice, y, rot, tran, cumulative_weights, dec_mask, optim, beta, beta_control=None, ctf_params=None, use_amp=False):
    optim.zero_grad()
    model.train()

    B, ntilts, D, D = y.shape

    with autocast(enabled=use_amp):
        if not torch.all(tran == 0):
            y = lattice.translate_ht(y.view(B * ntilts, -1), tran.view(B * ntilts, 1, 2))
        y = y.view(B, ntilts, D, D)

        z_mu, z_logvar, z, y_recon, ctf_weights = run_batch(model, lattice, y, rot, dec_mask, B, ntilts, D, ctf_params)
        loss, gen_loss, kld = loss_function(z_mu, z_logvar, y, y_recon, cumulative_weights, dec_mask, beta, beta_control)

    scaler.scale(loss).backward()
    scaler.step(optim)
    scaler.update()

    return loss.item(), gen_loss.item(), kld.item()


def run_batch(model, lattice, y, rot, dec_mask, B, ntilts, D, ctf_params=None):
    # encode
    input = y.clone()

    if not torch.all(ctf_params == 0):
        # phase flip the CTF-corrupted image
        freqs = lattice.freqs2d.unsqueeze(0).expand(B * ntilts, -1, -1) / ctf_params[:,:,1].view(B * ntilts, 1, 1)
        ctf_weights = ctf.compute_ctf(freqs, *torch.split(ctf_params.view(B*ntilts,-1)[:, 2:], 1, 1)).view(B, ntilts, D, D)
        input *= ctf_weights.sign() # phase flip by the ctf to be all positive amplitudes
    else:
        ctf_weights = None

    z_mu, z_logvar = _unparallelize(model).encode(input, B, ntilts) # ouput is B x zdim, i.e. one value per ptcl (not per img)
    z = _unparallelize(model).reparameterize(z_mu, z_logvar)

    # decode
    # model decoding is not vectorized across batch dimension due to uneven numbers of coords from each ptcl to reconstruct after skip_zeros_decoder masking of sample_ntilts different images per ptcl
    # TODO reimplement decoding more cleanly and efficiently with NestedTensors when autograd available: https://github.com/pytorch/nestedtensor
    input_coords = (lattice.coords @ rot).view(B, ntilts, D, D, 3)[dec_mask, :].unsqueeze(0)  # shape 1 x dec_mask.flat() x 3, because dec_mask can have variable # elements per particle
    ptcl_pixel_counts = [int(i) for i in torch.sum(dec_mask.view(B, -1), dim=1)]
    img_pixel_counts = [[int(i) for i in torch.sum(dec_mask[j].view(ntilts, -1), dim=1)] for j in range(B)]

    # slicing by #pixels per particle after dec_mask is applied to indirectly get regularly sized tensors for model evaluation
    y_recon = [model(input_coords_ptcl, z[i].unsqueeze(0), img_pixel_counts = img_pixel_counts[i]) for i, input_coords_ptcl in enumerate(input_coords.split(ptcl_pixel_counts, dim=1))]
    y_recon = torch.cat(y_recon, dim=1)

    if ctf_weights is not None:
        y_recon *= ctf_weights[dec_mask].view(1,-1)

    return z_mu, z_logvar, z, y_recon, ctf_weights


def _unparallelize(model):
    if isinstance(model, nn.DataParallel):
        return model.module
    return model


def loss_function(z_mu, z_logvar, y, y_recon, cumulative_weights, dec_mask, beta, beta_control=None):
    # reconstruction error
    y = y[dec_mask].view(1,-1)
    y_recon *= cumulative_weights[dec_mask].view(1,-1)
    gen_loss = ((y_recon - y) ** 2).mean()


    # latent loss
    kld = torch.mean(-0.5 * torch.sum(1 + z_logvar - z_mu.pow(2) - z_logvar.exp(), dim=1), dim=0)

    # total loss
    if beta_control is None:
        loss = gen_loss + beta * kld / dec_mask.sum().float()
    else:
        loss = gen_loss + beta_control * (beta - kld)**2 / dec_mask.sum().float()
    return loss, gen_loss, kld


def eval_z(model, lattice, data, batch_size, device):
    assert not model.training
    z_mu_all = []
    z_logvar_all = []
    data_generator = DataLoader(data, batch_size=batch_size, shuffle=False)
    for batch_images, _, _, batch_ctf, _, _, _ in data_generator:
        y = batch_images.to(device)
        B = y.size(0)
        ntilts = y.size(1)
        D = lattice.D
        y = y.view(B, ntilts, D, D)
        if batch_ctf is not None:
            freqs = lattice.freqs2d.unsqueeze(0).expand(B * ntilts, -1, -1) / batch_ctf[:,:,1].view(B * ntilts, 1, 1)
            ctf_weights = ctf.compute_ctf(freqs, *torch.split(batch_ctf.view(B*ntilts,-1)[:, 2:], 1, 1)).view(B, ntilts, D, D)
            y *= ctf_weights.sign() # phase flip by the ctf
        z_mu, z_logvar = _unparallelize(model).encode(y, B, ntilts)
        z_mu_all.append(z_mu.detach().cpu().numpy())
        z_logvar_all.append(z_logvar.detach().cpu().numpy())
    z_mu_all = np.vstack(z_mu_all)
    z_logvar_all = np.vstack(z_logvar_all)
    return z_mu_all, z_logvar_all


def save_checkpoint(model, optim, epoch, z_mu, z_logvar, out_weights, out_z, scaler):
    '''
    Save model weights and latent encoding z
    '''
    # save model weights
    torch.save({
        'epoch':epoch,
        'model_state_dict':_unparallelize(model).state_dict(),
        'optimizer_state_dict':optim.state_dict(),
        'scaler': scaler.state_dict() if scaler is not None else None
        }, out_weights)
    # save z
    with open(out_z,'wb') as f:
        pickle.dump(z_mu, f)
        pickle.dump(z_logvar, f)


def main(args):
    t1 = dt.now()
    if args.outdir is not None and not os.path.exists(args.outdir):
        os.makedirs(args.outdir)
    LOG = f'{args.outdir}/run.log'

    def flog(msg): # HACK: switch to logging module
        return utils.flog(msg, LOG)

    if args.load == 'latest':
        args = get_latest(args)
    flog(' '.join(sys.argv))

    # set the random seed
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    # set the device
    use_cuda = torch.cuda.is_available()
    device = torch.device('cuda' if use_cuda else 'cpu')
    flog('Use cuda {}'.format(use_cuda))
    if use_cuda:
        torch.set_default_tensor_type(torch.cuda.FloatTensor)
    else:
        log('WARNING: No GPUs detected')

    # set beta schedule
    if args.beta is None:
        args.beta = 1./args.zdim
    try:
        args.beta = float(args.beta)
    except ValueError:
        assert args.beta_control, "Need to set beta control weight for schedule {}".format(args.beta)
    beta_schedule = get_beta_schedule(args.beta)

    # load the particle indices
    if args.ind is not None:
        flog(f'Reading supplied particle indices {args.ind}')
        ind = pickle.load(open(args.ind, 'rb'))
    else:
        ind = None

    # load the particles + poses + ctf from input starfile
    flog(f'Loading dataset from {args.particles}')
    data = dataset.TiltSeriesDatasetMaster(args.particles, norm=args.norm, invert_data=args.invert_data,
                                           ind_ptcl=ind, window=args.window, datadir=args.datadir,
                                           window_r=args.window_r, recon_dose_weight=args.recon_dose_weight,
                                           dose_override=args.dose_override, recon_tilt_weight=args.recon_tilt_weight,
                                           l_dose_mask=args.l_dose_mask, lazy=args.lazy)
    D = data.D
    nptcls = data.nptcls
    Apix = data.ptcls[data.ptcls_list[0]].ctf[0,1] if not np.all(data.ptcls[data.ptcls_list[0]].ctf == 0) else args.Apix
    flog(f'Pixels per particle: {data.ptcls[data.ptcls_list[0]].D ** 2 * data.ptcls[data.ptcls_list[0]].ntilts} ')

    # instantiate lattice
    lattice = Lattice(D, extent=args.l_extent, device=device)

    # determine which pixels to encode (equivalently applicable to all particles)
    if args.enc_mask is None:
        args.enc_mask = D // 2
    if args.enc_mask > 0:
        # encode pixels within defined circular radius in fourier space
        assert args.enc_mask <= D // 2
        enc_mask = lattice.get_circular_mask(args.enc_mask)
        in_dim = enc_mask.sum()
    elif args.enc_mask == -1:
        # encode all pixels in fourier space
        enc_mask = None
        in_dim = lattice.D ** 2 if not args.use_real else (lattice.D - 1) ** 2
    else:
        raise RuntimeError("Invalid argument for encoder mask radius {}".format(args.enc_mask))
    flog(f'Pixels encoded per tilt (+ enc-mask):  {in_dim.sum()}')

    # determine which pixels to decode (cache by related weights_dict keys)
    flog(f'Constructing baseline circular lattice for decoder with radius {lattice.D // 2}')
    lattice_mask = lattice.get_circular_mask(lattice.D // 2) # reconstruct box-inscribed circle of pixels
    for i, weights_key in enumerate(data.weights_dict.keys()):
        dec_mask = (data.dec_mask_dict[weights_key] * lattice_mask.view(1, D, D).cpu().numpy()).astype(dtype=bool)  # lattice mask excludes DC and > nyquist frequencies; weights mask excludes > optimal dose
        data.dec_mask_dict[weights_key] = dec_mask
        flog(f'Pixels decoded per particle (--l-dose-mask: {args.l_dose_mask} and --l-extent: {args.l_extent}, mask scheme: {i}):  {dec_mask.sum()}')

        # save plots of all weighting schemes
        weights = data.weights_dict[weights_key]
        spatial_frequencies = data.spatial_frequencies
        dose.plot_weight_distribution(weights * dec_mask, spatial_frequencies, args.outdir, weight_distribution_index=i)

    if args.sample_ntilts:
        assert args.sample_ntilts <= data.ntilts_range[0], \
            f'The number of tilts requested to be sampled per particle ({args.sample_ntilts}) ' \
            f'exceeds the number of tilt images for at least one particle ({data.ntilts_range[0]})'
        data.ntilts_training = args.sample_ntilts
        flog(f'Sampling {args.sample_ntilts} tilts per particle')
    else:
        data.ntilts_training = data.ntilts_range[0]

    # instantiate model
    activation = {"relu": nn.ReLU, "leaky_relu": nn.LeakyReLU}[args.activation]
    model = TiltSeriesHetOnlyVAE(lattice, args.qlayersA, args.qdimA, args.out_dim_A, data.ntilts_training,
                                 args.qlayersB, args.qdimB, args.players, args.pdim, in_dim, args.zdim,
                                 enc_mask=enc_mask, enc_type=args.pe_type, enc_dim=args.pe_dim,
                                 domain='fourier', activation=activation, use_amp=args.amp,
                                 l_dose_mask=args.l_dose_mask, feat_sigma=args.feat_sigma,
                                 use_decoder_symmetry=args.use_decoder_symmetry)
    model.to(device)
    flog(model)
    flog('{} parameters in model'.format(sum(p.numel() for p in model.parameters() if p.requires_grad)))
    flog('{} parameters in encoder'.format(sum(p.numel() for p in model.encoder.parameters() if p.requires_grad)))
    flog('{} parameters in decoder'.format(sum(p.numel() for p in model.decoder.parameters() if p.requires_grad)))

    # save configuration
    out_config = f'{args.outdir}/config.pkl'
    save_config(args, data, lattice, model, out_config)

    # instantiate optimizer
    optim = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.wd)

    # Mixed precision training with AMP
    use_amp = not args.no_amp
    flog(f'AMP acceleration enabled (autocast + gradscaler) : {use_amp}')
    scaler = GradScaler(enabled=use_amp)
    if use_amp:
        if not args.batch_size % 8 == 0: flog('Warning: recommended to have batch size divisible by 8 for AMP training')
        if not (D-1) % 8 == 0: flog('Warning: recommended to have image size divisible by 8 for AMP training')
        if in_dim % 8 != 0: flog('Warning: recommended to have masked image dimensions divisible by 8 for AMP training')
        assert args.qdimA % 8 == 0, "Encoder hidden layer dimension must be divisible by 8 for AMP training"
        assert args.qdimB % 8 == 0, "Encoder hidden layer dimension must be divisible by 8 for AMP training"
        if args.zdim % 8 != 0: flog('Warning: recommended to have z dimension divisible by 8 for AMP training')
        assert args.pdim % 8 == 0, "Decoder hidden layer dimension must be divisible by 8 for AMP training"

    # restart from checkpoint
    if args.load:
        flog('Loading checkpoint from {}'.format(args.load))
        checkpoint = torch.load(args.load)
        model.load_state_dict(checkpoint['model_state_dict'])
        optim.load_state_dict(checkpoint['optimizer_state_dict'])
        try:
            scaler.load_state_dict(checkpoint['scaler'])
        except:
            flog('No GradScaler instance found in specified checkpoint; creating new GradScaler')
        start_epoch = checkpoint['epoch']+1
        model.train()
    else:
        start_epoch = 0

    # parallelize
    if args.multigpu and torch.cuda.device_count() > 1:
        log(f'Using {torch.cuda.device_count()} GPUs!')
        args.batch_size *= torch.cuda.device_count()
        log(f'Increasing batch size to {args.batch_size}')
        model = nn.DataParallel(model)
    elif args.multigpu:
        log(f'WARNING: --multigpu selected, but {torch.cuda.device_count()} GPUs detected')

    # train
    flog('Done all preprocessing; starting training now!')
    data_generator = DataLoader(data, batch_size=args.batch_size, shuffle=True)
    for epoch in range(start_epoch, args.num_epochs):
        t2 = dt.now()
        gen_loss_accum = 0
        loss_accum = 0
        kld_accum = 0
        batch_it = 0
        for batch_images, batch_rot, batch_trans, batch_ctf, batch_weights, batch_dec_mask, batch_ptcl_ids in data_generator:
            # impression counting
            batch_it += len(batch_ptcl_ids) # total number of ptcls seen
            global_it = nptcls*epoch + batch_it
            beta = beta_schedule(global_it)

            # training minibatch
            loss, gen_loss, kld = train_batch(scaler, model, lattice, batch_images, batch_rot, batch_trans,
                                              batch_weights, batch_dec_mask, optim, beta, args.beta_control,
                                              ctf_params=batch_ctf, use_amp=use_amp)

            # logging
            gen_loss_accum += gen_loss * len(batch_ptcl_ids)
            kld_accum += kld * len(batch_ptcl_ids)
            loss_accum += loss * len(batch_ptcl_ids)

            if batch_it % args.log_interval == 0:
                log(f'# [Train Epoch: {epoch+1}/{args.num_epochs}] [{batch_it}/{nptcls} subtomos] gen loss={gen_loss:.6f}, kld={kld:.6f}, beta={beta:.6f}, loss={loss:.6f}')

        flog(f'# =====> Epoch: {epoch+1} Average gen loss = {gen_loss_accum/batch_it:.6}, KLD = {kld_accum/batch_it:.6f}, total loss = {loss_accum/batch_it:.6f}; Finished in {dt.now()-t2}')
        if args.checkpoint and epoch % args.checkpoint == 0:
            flog(f'GPU memory usage: {utils.check_memory_usage()}')
            out_weights = '{}/weights.{}.pkl'.format(args.outdir,epoch)
            out_z = '{}/z.{}.pkl'.format(args.outdir, epoch)
            model.eval()
            with torch.no_grad():
                z_mu, z_logvar = eval_z(model, lattice, data, args.batch_size, device)
                save_checkpoint(model, optim, epoch, z_mu, z_logvar, out_weights, out_z, scaler)

    # save model weights, latent encoding, and evaluate the model on 3D lattice
    out_weights = '{}/weights.pkl'.format(args.outdir)
    out_z = '{}/z.pkl'.format(args.outdir)
    model.eval()
    with torch.no_grad():
        z_mu, z_logvar = eval_z(model, lattice, data, args.batch_size, device)
        save_checkpoint(model, optim, epoch, z_mu, z_logvar, out_weights, out_z, scaler)

    td = dt.now() - t1
    flog('Finished in {} ({} per epoch)'.format(td, td / (args.num_epochs - start_epoch)))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    args = add_args(parser).parse_args()
    utils._verbose = args.verbose
    main(args)



