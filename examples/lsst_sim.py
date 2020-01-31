#!/usr/bin/env python
"""
    - do a straight coadd and store it in an ngmix Observation for the
    simple_sim, which makes perfectly registered images with a pixel scale and
    same psf and wcs for all images

    - run detection and deblending
    - run a stub for measurement on deblended images (not doing anything yet)
    - optionally make a plot and overplot detections on the image
"""
import os
import sys
import logging
import numpy as np
# import matplotlib.pyplot as plt
import ngmix

from descwl_shear_sims import Sim
from descwl_coadd.coadd import MultiBandCoadds
from descwl_coadd.coadd_simple import MultiBandCoaddsSimple
from metadetect.lsst_metadetect import LSSTMetadetect
from metadetect.metadetect import Metadetect
import fitsio
import esutil as eu
import argparse


def make_comb_data(res):
    add_dt = [('shear_type', 'S7')]

    dlist = []
    for stype in res.keys():
        data = res[stype]
        if data is not None:
            newdata = eu.numpy_util.add_fields(data, add_dt)
            newdata['shear_type'] = stype
            dlist.append(newdata)

    if len(dlist) > 0:
        return eu.numpy_util.combine_arrlist(dlist)
    else:
        return []


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--ntrial', type=int, default=1)
    parser.add_argument('--seed', type=int)
    parser.add_argument('--noise', type=float, default=180)
    parser.add_argument('--nepochs', type=int, default=3)
    parser.add_argument('--cosmic-rays', action='store_true')
    parser.add_argument('--bad-columns', action='store_true')

    parser.add_argument('--psf-g1', type=float, default=0)
    parser.add_argument('--psf-g2', type=float, default=0)

    parser.add_argument('--rotate', action='store_true')
    parser.add_argument('--dither', action='store_true')
    parser.add_argument('--vary-scale', action='store_true')
    parser.add_argument('--vary-wcs-shear', action='store_true')

    parser.add_argument('--coadd-dim', type=int, default=350)
    parser.add_argument('--buff', type=int, default=50)

    parser.add_argument('--grid', action='store_true')
    parser.add_argument('--grid-gals', type=int, default=9)

    parser.add_argument('--show', action='store_true')

    parser.add_argument('--nostack', action='store_true',
                        help=('just do weighted sum coadd and run '
                              'metadetect'))

    parser.add_argument('--bands', default='r,i,z')
    return parser.parse_args()


def show_sim(data):
    """
    show an image
    """
    from descwl_coadd.vis import show_images, show_2images

    images = []
    for band in data:
        for se_obs in data[band]:
            images.append(se_obs.image.array)
            images.append(se_obs.get_psf(25.1, 31.5).array)

    if len(images) == 2:
        show_2images(*images)
    else:
        show_images(images)


def get_sim_kw(args):

    wcs_kws = {}

    if args.rotate:
        assert not args.nostack
        wcs_kws['position_angle_range'] = (0, 360)

    if args.dither:
        assert not args.nostack
        wcs_kws['dither_range'] = (-0.5, 0.5)

    if args.vary_scale:
        assert not args.nostack
        wcs_kws['scale_frac_std'] = 0.01

    if args.vary_wcs_shear:
        assert not args.nostack
        wcs_kws['shear_std'] = 0.01

    if args.cosmic_rays or args.bad_columns:
        assert not args.nostack

    bands = args.bands.split(',')

    sim_kw = dict(
        bands=bands,
        epochs_per_band=args.nepochs,
        noise_per_band=args.noise,
        wcs_kws=wcs_kws,
        cosmic_rays=args.cosmic_rays,
        bad_columns=args.bad_columns,
        coadd_dim=args.coadd_dim,
        buff=args.buff,
    )
    if args.grid:
        sim_kw['grid_gals'] = True
        sim_kw['ngals'] = args.grid_gals  # really means NxN

    return sim_kw


def get_config(args):
    config = {
        'bmask_flags': 0,
        'metacal': {
            'use_noise_image': True,
            'psf': 'fitgauss',
        },
        'psf': {
            'model': 'gauss',
            'lm_pars': {},
            'ntry': 2,
        },
        'weight': {
            'fwhm': 1.2,
        },
        'meds': {},
    }

    if args.nostack:
        config['sx'] = {
            # in sky sigma
            # DETECT_THRESH
            'detect_thresh': 0.8,

            # Minimum contrast parameter for deblending
            # DEBLEND_MINCONT
            'deblend_cont': 0.00001,

            # minimum number of pixels above threshold
            # DETECT_MINAREA: 6
            'minarea': 4,

            'filter_type': 'conv',

            # 7x7 convolution mask of a gaussian PSF with FWHM = 3.0 pixels.
            'filter_kernel':  [
                [0.004963, 0.021388, 0.051328, 0.068707, 0.051328, 0.021388, 0.004963],  # noqa
                [0.021388, 0.092163, 0.221178, 0.296069, 0.221178, 0.092163, 0.021388],  # noqa
                [0.051328, 0.221178, 0.530797, 0.710525, 0.530797, 0.221178, 0.051328],  # noqa
                [0.068707, 0.296069, 0.710525, 0.951108, 0.710525, 0.296069, 0.068707],  # noqa
                [0.051328, 0.221178, 0.530797, 0.710525, 0.530797, 0.221178, 0.051328],  # noqa
                [0.021388, 0.092163, 0.221178, 0.296069, 0.221178, 0.092163, 0.021388],  # noqa
                [0.004963, 0.021388, 0.051328, 0.068707, 0.051328, 0.021388, 0.004963],  # noqa
            ]
        }

        config['meds'] = {
            'min_box_size': 32,
            'max_box_size': 256,

            'box_type': 'iso_radius',

            'rad_min': 4,
            'rad_fac': 2,
            'box_padding': 2,
        }
        # fraction of slice where STAR or TRAIL was set.  We may cut objects
        # detected there
        config['star_flags'] = 96

        # we don't interpolate over tapebumps
        config['tapebump_flags'] = 16384

        # things interpolated using the spline
        config['spline_interp_flags'] = 3155

        # replaced with noise
        config['noise_interp_flags'] = 908

        # pixels will have these flag set in the ormask if they were
        # interpolated plus adding in tapebump and star
        config['imperfect_flags'] = 20479

    return config


def make_mbobs(obs, psf_fwhm):
    mbobs = ngmix.MultiBandObsList(
        meta={'psf_fwhm': psf_fwhm}
    )
    obslist = ngmix.ObsList()
    obslist.append(obs)
    mbobs.append(obslist)
    return mbobs


def main():

    args = get_args()

    rng = np.random.RandomState(args.seed)
    config = get_config(args)

    logging.basicConfig(stream=sys.stdout)
    logging.getLogger('descwl_shear_testing').setLevel(
        getattr(logging, 'INFO')
    )

    dlist_p = []
    dlist_m = []

    for trial in range(args.ntrial):
        print('-'*70)
        print('trial: %d/%d' % (trial+1, args.ntrial))

        trial_seed = rng.randint(0, 2**30)

        for shear_type in ('1p', '1m'):
            print(shear_type)

            sim_kw = get_sim_kw(args)

            trial_rng = np.random.RandomState(trial_seed)

            if shear_type == '1p':
                sim_kw['g1'] = 0.02
            else:
                sim_kw['g1'] = -0.02

            sim_kw['psf_kws'] = {'g1': args.psf_g1, 'g2': args.psf_g2}
            sim_kw['rng'] = trial_rng
            sim = Sim(**sim_kw)
            data = sim.gen_sim()

            if args.show:
                show_sim(data)

            if args.nostack:
                coadd_obs = MultiBandCoaddsSimple(data=data)

                coadd_mbobs = make_mbobs(coadd_obs, sim.psf_kws['fwhm'])
                md = Metadetect(
                    config,
                    coadd_mbobs,
                    trial_rng,
                    show=args.show,
                )
            else:

                psf_dim = int(sim.psf_dim/np.sqrt(3))
                if psf_dim % 2 == 0:
                    psf_dim -= 1

                mbc = MultiBandCoadds(
                    data=data,
                    coadd_wcs=sim.coadd_wcs,
                    coadd_dims=[sim.coadd_dim]*2,
                    psf_dims=[psf_dim]*2,
                    byband=False,
                    show=args.show,
                )

                coadd_obs = mbc.coadds['all']
                coadd_mbobs = make_mbobs(coadd_obs, sim.psf_kws['fwhm'])

                md = LSSTMetadetect(
                    config,
                    coadd_mbobs,
                    trial_rng,
                    show=args.show,
                )

            md.go()
            res = md.result
            # print(res.keys())

            comb_data = make_comb_data(res)
            if len(comb_data) > 0:
                if shear_type == '1p':
                    dlist_p.append(comb_data)
                else:
                    dlist_m.append(comb_data)

    data_1p = eu.numpy_util.combine_arrlist(dlist_p)
    data_1m = eu.numpy_util.combine_arrlist(dlist_m)

    print('writing:', args.output)
    with fitsio.FITS(args.output, 'rw', clobber=True) as fits:
        fits.write(data_1p, extname='1p')
        fits.write(data_1m, extname='1m')


if __name__ == '__main__':
    os.environ['OMP_NUM_THREADS'] = '1'
    main()