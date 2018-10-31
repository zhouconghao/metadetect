"""
test with super simple sim.  The purpose here is not
to make sure it gets the right answer or anything, just
to test all the moving parts
"""
import numpy as np
import ngmix
from . import detect
from . import fitting

DEFAULT_CONFIG={
    'nobj': 4,
    'nband': 3,
    'noises': (0.0005,0.001,0.0015),
    'scale': 0.263,
    'psf_fwhm': 0.9,
    'dims': (64,64),
    'flux_low': 0.5,
    'flux_high': 1.5,
    'r50_low': 0.1,
    'r50_high': 2.0,
    'g_std':0.2,
    'fracdev_low': 0.001,
    'fracdev_high': 0.99,
    'bulge_colors': np.array([0.5, 1.0, 1.5]),
    'disk_colors': np.array([1.25, 1.0, 0.75]),
}

class Sim(dict):
    def __init__(self, rng, config=None):
        self.update(DEFAULT_CONFIG)

        if config is not None:
            self.update(config)

        self['pos_width'] = self['dims'][0]/2.0*0.6 * self['scale']
        self.rng=rng

        self._set_wcs()
        self._make_psf()
        self._gpdf=ngmix.priors.GPriorBA(
            self['g_std'],
            rng=self.rng,
        )

    def get_mbobs(self):
        """
        get a simulated MultiBandObsList
        """
        import galsim
        all_band_obj = self._get_band_objects()

        dlist=[]
        mbobs=ngmix.MultiBandObsList()
        for band in range(self['nband']):
            band_objects = [ o[band] for o in all_band_obj ]
            obj = galsim.Sum(band_objects)

            im = obj.drawImage(
                nx=self['dims'][1],
                ny=self['dims'][0],
                scale=self['scale']
            ).array

            im += self.rng.normal(scale=self['noises'][band], size=im.shape)
            wt = im*0 + 1.0/self['noises'][band]**2
            bmask = np.zeros(im.shape,dtype='i4')

            obs = ngmix.Observation(
                im,
                weight=wt,
                bmask=bmask,
                jacobian=self._jacobian,
                psf=self._psf_obs.copy(),
            )

            obslist=ngmix.ObsList()
            obslist.append(obs)
            mbobs.append(obslist)

        return mbobs


    def _get_r50(self):
        return self.rng.uniform(
            low=self['r50_low'],
            high=self['r50_high'],
        )
    def _get_flux(self):
        return self.rng.uniform(
            low=self['flux_low'],
            high=self['flux_high'],
        )
    def _get_fracdev(self):
        return self.rng.uniform(
            low=self['fracdev_low'],
            high=self['fracdev_high'],
        )

    def _get_g(self):
        return self._gpdf.sample2d()

    def _get_dxdy(self):
        return self.rng.uniform(
            low  =-self['pos_width'],
            high = self['pos_width'],
            size=2,
        )

    def _get_band_objects(self):
        import galsim

        all_band_obj=[]
        for i in range(self['nobj']):
            r50=self._get_r50()
            flux = self._get_flux()
            fracdev = self._get_fracdev()
            dx,dy=self._get_dxdy()

            g1d,g2d=self._get_g()
            g1b=0.5*g1d
            g2b=0.5*g2d

            flux_bulge = fracdev*flux
            flux_disk  = (1-fracdev)*flux
            print("fracdev:",fracdev)

            bulge_obj = galsim.DeVaucouleurs(
                half_light_radius=r50
            ).shear(g1=g1b,g2=g2b)

            disk_obj = galsim.Exponential(
                half_light_radius=r50
            ).shear(g1=g1d,g2=g2d)

            band_objs = []
            for band in range(self['nband']):
                band_disk=disk_obj.withFlux(flux_disk*self['disk_colors'][band])
                band_bulge=bulge_obj.withFlux(flux_bulge*self['bulge_colors'][band])

                obj = galsim.Sum(band_disk, band_bulge).shift(dx=dx, dy=dy)
                obj=galsim.Convolve(obj, self._psf)
                band_objs.append( obj )

            all_band_obj.append( band_objs )

        return all_band_obj

    def _set_wcs(self):
        self._jacobian=ngmix.DiagonalJacobian(
            row=0,
            col=0,
            scale=self['scale'],
        )

    def _make_psf(self):
        import galsim

        self._psf=galsim.Gaussian(fwhm=self['psf_fwhm'])

        psf_im = self._psf.drawImage(scale=self['scale']).array
        noise = psf_im.max()/1000.0
        weight = psf_im + 1.0/noise**2
        psf_im += self.rng.normal(
            scale=noise,
            size=psf_im.shape
        )

        cen=(np.array(psf_im.shape)-1.0)/2.0
        j=self._jacobian.copy()
        j.set_cen(row=cen[0], col=cen[1])

        psf_obs = ngmix.Observation(
            psf_im,
            weight=weight,
            jacobian=j
        )
        self._psf_obs = psf_obs

def _show_mbobs(mer):
    import biggles
    import images

    mbobs=mer.mbobs

    tab=biggles.Table(1,2)
    rgb=images.get_color_image(
        mbobs[2][0].image.transpose(),
        mbobs[1][0].image.transpose(),
        mbobs[0][0].image.transpose(),
        nonlinear=0.1,
    )
    rgb *= 1.0/rgb.max()
 
    plt = images.view_mosaic(
        [rgb,
         mer.seg,
         mer.detim],
        titles=['image','seg','detim'],
    )


def test(ntrial=1, dim=2000, show=False):
    import biggles
    import images
    import time

    rng=np.random.RandomState()

    tm0 = time.time()
    nobj_meas = 0

    sim = Sim(rng)

    for trial in range(ntrial):
        print("trial: %d/%d" % (trial+1,ntrial))

        mbobs = sim.get_mbobs()
        mer=detect.MEDSifier(mbobs)

        mbm=mer.get_multiband_meds()

        nobj=mbm.size
        print("        found",nobj,"objects")
        nobj_meas += nobj

        if show:
            _show_mbobs(mer)
            if ntrial > 1 and trial != (ntrial-1):
                if 'q'==input("hit a key: "):
                    return


    total_time=time.time()-tm0
    print("time per group:",total_time/ntrial)
    print("time per object:",total_time/nobj_meas)