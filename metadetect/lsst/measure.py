import logging
import warnings
import numpy as np
import esutil as eu
import ngmix

import lsst.afw.table as afw_table
from lsst.meas.algorithms import SourceDetectionTask, SourceDetectionConfig
from lsst.meas.deblender import SourceDeblendTask, SourceDeblendConfig
from lsst.meas.base import (
    SingleFrameMeasurementConfig,
    SingleFrameMeasurementTask,
)
import lsst.geom as geom
from lsst.pex.exceptions import (
    InvalidParameterError,
    LengthError,
)

from ..procflags import (
    EDGE_HIT, ZERO_WEIGHTS, CENTROID_FAILURE, NO_ATTEMPT,
)
from ..fitting import fit_mbobs_wavg, get_wavg_output_struct

from . import util
from .util import ContextNoiseReplacer
from . import vis
from .defaults import DEFAULT_THRESH

warnings.filterwarnings('ignore', category=FutureWarning)

LOG = logging.getLogger('lsst_measure')


def detect_and_deblend(
    mbexp,
    rng,
    thresh=DEFAULT_THRESH,
    show=False,
):
    """
    run detection and deblending of peaks, as well as basic measurments such as
    centroid.  The SDSS deblender is run in order to split footprints.

    We must combine detection and deblending in the same function because the
    schema gets modified in place, which means we must construct the deblend
    task at the same time as the detect task

    Parameters
    ----------
    mbexp: lsst.afw.image.MultibandExposure
        The exposures to process
    rng: np.random.RandomState
        Random number generator for noise replacer
    thresh: float, optional
        The detection threshold in units of the sky noise
    show: bool, optional
        If set to True, show images

    Returns
    -------
    sources, detexp
        The sources and the detection exposure
    """
    import lsst.afw.image as afw_image

    if len(mbexp.singles) > 1:
        detexp = util.coadd_exposures(mbexp.singles)
    else:
        detexp = mbexp.singles[0]

    # background measurement within the detection code requires ExposureF
    if not isinstance(detexp, afw_image.ExposureF):
        detexp = afw_image.ExposureF(detexp, deep=True)

    schema = afw_table.SourceTable.makeMinimalSchema()

    # Setup algorithms to run
    meas_config = SingleFrameMeasurementConfig()
    meas_config.plugins.names = [
        "base_SdssCentroid",
        "base_PsfFlux",
        "base_SkyCoord",
    ]

    # set these slots to none because we aren't running these algorithms
    meas_config.slots.apFlux = None
    meas_config.slots.gaussianFlux = None
    meas_config.slots.calibFlux = None
    meas_config.slots.modelFlux = None

    # goes with SdssShape above
    meas_config.slots.shape = None

    # fix odd issue where it things things are near the edge
    meas_config.plugins['base_SdssCentroid'].binmax = 1

    meas_task = SingleFrameMeasurementTask(
        config=meas_config,
        schema=schema,
    )

    detection_config = SourceDetectionConfig()
    detection_config.reEstimateBackground = False
    # variance here actually means relative to the sqrt(variance)
    # from the variance plane.
    # TODO this would include poisson
    # TODO detection doesn't work right when we tell it to trust
    # the variance
    # detection_config.thresholdType = 'variance'
    detection_config.thresholdValue = thresh

    # these will be ignored when finding the image standard deviation
    detection_config.statsMask = util.get_stats_mask(detexp)

    detection_task = SourceDetectionTask(config=detection_config)

    # these tasks must use the same schema and all be constructed before any
    # other tasks using the same schema are run because schema is modified in
    # place by tasks, and the constructor does a check that fails if we do this
    # afterward

    deblend_task = SourceDeblendTask(
        config=SourceDeblendConfig(),
        schema=schema,
    )

    table = afw_table.SourceTable.make(schema)

    result = detection_task.run(table, detexp)

    if show:
        vis.show_exp(detexp)

    if result is not None:
        sources = result.sources
        deblend_task.run(detexp, sources)

        with ContextNoiseReplacer(detexp, sources, rng) as replacer:

            for source in sources:

                if source.get('deblend_nChild') != 0:
                    continue

                source_id = source.getId()

                with replacer.sourceInserted(source_id):
                    meas_task.callMeasure(source, detexp)

    else:
        sources = []

    return sources, detexp


def measure(
    mbexp,
    detexp,
    sources,
    fitter,
    stamp_size,
):
    """
    run measurements on the input exposure, given the input measurement task,
    list of sources, and fitter.

    Parameters
    ----------
    mbexp: lsst.afw.image.MultibandExposure
        The exposure on which to detect and measure
    detexp: lsst.afw.image.Exposure*
        The detection exposure, used for bmask info
    sources: list of sources
        From a detection task
    fitter: e.g. ngmix.gaussmom.GaussMom or ngmix.ksigmamom.PGaussMom
        For calculating moments
    stamp_size: int
        Size for postage stamps

    Returns
    -------
    ndarray with results or None
    """

    if len(sources) == 0:
        return None

    exp_bbox = mbexp.getBBox()
    wcs = mbexp.singles[0].getWcs()
    results = []

    # bmasks will be different within the loop below due to the replacer
    bmasks = get_bmasks(sources=sources, exposure=detexp)

    for i, source in enumerate(sources):

        if source.get('deblend_nChild') != 0:
            continue

        bmask = bmasks[i]

        flags = 0
        try:
            mbobs = _get_stamp_mbobs(
                mbexp=mbexp, source=source, stamp_size=stamp_size,
            )

            # TODO do something with bmask_flags?
            # TODO implement nonshear_mbobs
            this_res = fit_mbobs_wavg(
                mbobs=mbobs,
                fitter=fitter,
                bmask_flags=0,
                nonshear_mbobs=None,
            )
        except LengthError as err:
            # This is raised when a bbox hits an edge
            LOG.debug('%s', err)
            flags = EDGE_HIT
        except AllZeroWeight as err:
            # failure creating some observation due to zero weights
            LOG.info('%s', err)
            flags = ZERO_WEIGHTS
        except CentroidFail as err:
            # failure in the center finding
            LOG.info(str(err))
            flags = CENTROID_FAILURE

        if flags != 0:
            this_res = get_wavg_output_struct(nband=1, model=fitter.kind)
            this_res['flags'] = flags

        res = get_output(
            wcs=wcs, source=source, res=this_res,
            bmask=bmask, stamp_size=stamp_size, exp_bbox=exp_bbox,
        )

        results.append(res)

    if len(results) > 0:
        results = eu.numpy_util.combine_arrlist(results)
    else:
        results = None

    return results


def get_bmasks(sources, exposure):
    """
    get a list of all the bmasks for the sources

    Parameters
    ----------
    sources: lsst.afw.table.SourceCatalog
        The sources
    exposure: lsst.afw.image.ExposureF
        The exposure

    Returns
    -------
    list of bmask values
    """
    bmasks = []
    for source in sources:
        bmask = get_bmask(source=source, exposure=exposure)
        bmasks.append(bmask)
    return bmasks


def get_bmask(source, exposure):
    """
    get bmask based on original peak position

    Parameters
    ----------
    sources: lsst.afw.table.SourceRecord
        The sources
    exposure: lsst.afw.image.ExposureF
        The exposure

    Returns
    -------
    bmask value
    """
    peak = source.getFootprint().getPeaks()[0]
    orig_cen = peak.getI()
    maskval = exposure.mask[orig_cen]
    return maskval


def extract_obs(exp, source):
    """
    convert an image object into an ngmix.Observation, including
    a psf observation

    parameters
    ----------
    imobj: lsst.afw.image.ExposureF
        The exposure
    source: lsst.afw.table.SourceRecord
        The source record

    returns
    --------
    obs: ngmix.Observation
        The Observation unless all the weight are zero, in which
        case AllZeroWeight is raised
    """

    im = exp.image.array

    wt = _extract_weight(exp)
    if np.all(wt <= 0):
        raise AllZeroWeight('all weights <= 0')

    bmask = exp.mask.array
    jacob = _extract_jacobian_at_source(
        exp=exp,
        source=source,
    )

    orig_cen = source.getCentroid()

    psf_im = extract_psf_image(exposure=exp, orig_cen=orig_cen)

    # fake the psf pixel noise
    psf_err = psf_im.max()*0.0001
    psf_wt = psf_im*0 + 1.0/psf_err**2

    # use canonical center for the psf
    psf_cen = (np.array(psf_im.shape)-1.0)/2.0
    psf_jacob = jacob.copy()
    psf_jacob.set_cen(row=psf_cen[0], col=psf_cen[1])

    # we will have need of the bit names which we can only
    # get from the mask object
    # this is sort of monkey patching, but I'm not sure of
    # a better solution

    meta = {'orig_cen': orig_cen}

    psf_obs = ngmix.Observation(
        psf_im,
        weight=psf_wt,
        jacobian=psf_jacob,
    )
    obs = ngmix.Observation(
        im,
        weight=wt,
        bmask=bmask,
        jacobian=jacob,
        psf=psf_obs,
        meta=meta,
    )

    return obs


def _get_stamp_mbobs(mbexp, source, stamp_size, clip=False):
    """
    Get a postage stamp MultibandExposure

    Parameters
    ----------
    mbexp: lsst.afw.image.MultibandExposure
        The exposures
    source: lsst.afw.table.SourceRecord
        The source for which to get the stamp
    stamp_size: int
        If sent, a bounding box is created with about this size rather than
        using the footprint bounding box. Typically the returned size is
        stamp_size + 1
    clip: bool, optional
        If set to True, clip the bbox to fit into the exposure.

        If clip is False and the bbox does not fit, a
        lsst.pex.exceptions.LengthError is raised

        Only relevant if stamp_size is sent.  Default False

    Returns
    -------
    lsst.afw.image.ExposureF
    """

    bbox = _get_bbox(mbexp, source, stamp_size, clip=clip)

    mbobs = ngmix.MultiBandObsList()
    for band in mbexp.filters:

        subexp = mbexp[band][bbox]
        obs = extract_obs(
            exp=subexp,
            source=source,
        )

        obslist = ngmix.ObsList(meta={'band': band})
        obslist.append(obs)
        mbobs.append(obslist)

    return mbobs


def _get_bbox(mbexp, source, stamp_size, clip=False):
    """
    Get a bounding box at the location of the specified source.

    Parameters
    ----------
    mbexp: lsst.afw.image.MultibandExposure
        The exposures
    source: lsst.afw.table.SourceRecord
        The source for which to get the stamp
    stamp_size: int
        If sent, a bounding box is created with about this size rather than
        using the footprint bounding box. Typically the returned size is
        stamp_size + 1
    clip: bool, optional
        If set to True, clip the bbox to fit into the exposure.

        If clip is False and the bbox does not fit, a
        lsst.pex.exceptions.LengthError is raised

        Only relevant if stamp_size is sent.  Default False

    Returns
    -------
    lsst.geom.Box2I
    """

    fp = source.getFootprint()
    peak = fp.getPeaks()[0]

    x_peak, y_peak = peak.getIx(), peak.getIy()

    bbox = geom.Box2I(
        geom.Point2I(x_peak, y_peak),
        geom.Extent2I(1, 1),
    )
    bbox.grow(stamp_size // 2)

    exp_bbox = mbexp.getBBox()
    if clip:
        bbox.clip(exp_bbox)
    else:
        if not exp_bbox.contains(bbox):
            source_id = source.getId()
            raise LengthError(
                f'requested stamp size {stamp_size} for source '
                f'{source_id} does not fit into the exposoure.  '
                f'Use clip=True to clip the bbox to fit'
            )

    return bbox


def extract_psf_image(exposure, orig_cen):
    """
    get the psf associated with this image.

    coadded psfs from DM are generally not square, but the coadd in cells code
    makes them so.  We will assert they are square and odd dimensions

    Parameters
    ----------
    exposure: lsst.afw.image.ExposureF
        The exposure data
    orig_cen: lsst.geom.Point2D
        The location at which to draw the image

    Returns
    -------
    ndarray
    """
    try:
        psfobj = exposure.getPsf()
        psfim = psfobj.computeKernelImage(orig_cen).array
    except InvalidParameterError:
        raise MissingDataError("could not reconstruct PSF")

    psfim = np.array(psfim, dtype='f4', copy=False)

    shape = psfim.shape
    assert shape[0] == shape[1], 'require square psf images'
    assert shape[0] % 2 != 0, 'require odd psf images'

    return psfim


def _extract_psf_image_fix(exposure, orig_cen):
    """
    get the psf associated with this image

    coadded psfs are generally not square, so we will
    trim it to be square and preserve the center to
    be at the new canonical center

    TODO: should we really trim the psf to be even?  will this
    cause a shift due being off-center?
    """
    try:
        psfobj = exposure.getPsf()
        psfim = psfobj.computeKernelImage(orig_cen).array
    except InvalidParameterError:
        raise MissingDataError("could not reconstruct PSF")

    psfim = np.array(psfim, dtype='f4', copy=False)

    psfim = util.trim_odd_image(psfim)
    return psfim


def _extract_weight(exp):
    """
    TODO get the estimated sky variance rather than this hack
    TODO should we zero out other bits?

    extract a weight map

    Areas with NO_DATA will get zero weight.

    Because the variance map includes the full poisson variance, which
    depends on the signal, we instead extract the median of the parts of
    the image without NO_DATA set

    parameters
    ----------
    exp: sub exposure object
    """

    # TODO implement bit checking
    var_image = exp.variance.array

    weight = var_image.copy()

    weight[:, :] = 0

    wuse = np.where(var_image > 0)

    if wuse[0].size > 0:
        medvar = np.median(var_image[wuse])
        weight[:, :] = 1.0/medvar
    else:
        print('    weight is all zero, found '
              'none that passed cuts')

    return weight


def _extract_jacobian_at_source(exp, source):
    """
    extract an ngmix.Jacobian from the image object
    and object record

    exp: lsst.afw.image.ExposureF
        An exposure object
    source: lsst.afw.table.SourceRecord
        The source record created during detection

    returns
    --------
    Jacobian: ngmix.Jacobian
        The local jacobian
    """
    from .util import get_jacobian

    orig_cen = exp.getWcs().skyToPixel(source.getCoord())

    if np.isnan(orig_cen.getY()):
        LOG.info('falling back on integer location')
        # fall back to integer pixel location
        peak = source.getFootprint().getPeaks()[0]
        orig_cen_i = peak.getI()
        orig_cen = geom.Point2D(
            x=orig_cen_i.getX(),
            y=orig_cen_i.getY(),
        )

    return get_jacobian(exp, orig_cen)


def get_output_dtype():

    dt = [
        ('stamp_size', 'i4'),
        ('row0', 'i4'),  # bbox row start
        ('col0', 'i4'),  # bbox col start
        ('row', 'f4'),  # row in image. Use row0 to get to global pixel coords
        ('col', 'f4'),  # col in image. Use col0 to get to global pixel coords
        ('row_diff', 'f4'),  # difference from peak location
        ('col_diff', 'f4'),  # difference from peak location
        ('row_noshear', 'f4'),  # noshear row in local image, not global wcs
        ('col_noshear', 'f4'),  # noshear col in local image, not global wcs
        ('ra', 'f8'),
        ('dec', 'f8'),

        ('psfrec_flags', 'i4'),  # psfrec is the original psf
        ('psfrec_g', 'f8', 2),
        ('psfrec_T', 'f8'),

        # values from .mask of input exposures
        ('bmask', 'i4'),
        # values for ormask across all input exposures to coadd
        ('ormask', 'i4'),
        # fraction of images going into a pixel that were masked
        ('mfrac', 'f4'),
    ]

    return dt


def get_output_struct(res):
    """
    get the output struct

    Parameters
    ----------
    res: ndarray
        The result from running metadetect.fitting.fit_mbobs_wavg

    Returns
    -------
    ndarray
        Has the fields from res, with new fields added, see get_output_dtype
    """
    dt = get_output_dtype()
    output = eu.numpy_util.add_fields(res, dt)

    for subdt in dt:
        name = subdt[0]
        dtype = subdt[1]

        if 'flags' in name:
            output[name] = NO_ATTEMPT
        elif name in ('bmask', 'ormask'):
            output[name] = 0
        elif dtype[0] == 'i':
            output[name] = -9999
        else:
            output[name] = np.nan

    return output


def get_output(wcs, source, res, bmask, stamp_size, exp_bbox):
    """
    get the output structure, copying in results

    The following fields are not set:
        row_noshear, col_noshear
        psfrec_flags, psfrec_g, psfrec_T
        mfrac

    Parameters
    ----------
    wcs: a stack wcs
        The wcs with which to determine the ra, dec
    res: ndarray
        The result from running metadetect.fitting.fit_mbobs_wavg
    bmask: int
        The bmask value at the location of this object
    stamp_size: int
        The stamp size used for the measurement
    exp_bbox: lsst.geom.Box2I
        The bounding box used for measurement

    Returns
    -------
    ndarray
        Has the fields from res, with new fields added, see get_output_dtype
    """
    import lsst.afw.image as afw_image

    output = get_output_struct(res)

    orig_cen = source.getCentroid()

    skypos = wcs.pixelToSky(orig_cen)

    peak = source.getFootprint().getPeaks()[0]
    peak_loc = peak.getI()

    if np.isnan(orig_cen.getY()):
        orig_cen = peak.getCentroid()
        cen_offset = geom.Point2D(np.nan, np.nan)
    else:
        cen_offset = geom.Point2D(
            orig_cen.getX() - peak_loc.getX(),
            orig_cen.getY() - peak_loc.getY(),
        )

    output['stamp_size'] = stamp_size
    output['row0'] = exp_bbox.getBeginY()
    output['col0'] = exp_bbox.getBeginX()
    output['row'] = orig_cen.getY()
    output['col'] = orig_cen.getX()
    output['row_diff'] = cen_offset.getY()
    output['col_diff'] = cen_offset.getX()

    output['ra'] = skypos.getRa().asDegrees()
    output['dec'] = skypos.getDec().asDegrees()

    # remove DETECTED bit, it is just clutter since all detected
    # objects have this bit set
    detected = afw_image.Mask.getPlaneBitMask('DETECTED')
    output['bmask'] = bmask & ~detected

    return output


class MissingDataError(Exception):
    """
    Some number was out of range
    """

    def __init__(self, value):
        super().__init__(value)
        self.value = value

    def __str__(self):
        return repr(self.value)


class AllZeroWeight(Exception):
    """
    Some number was out of range
    """

    def __init__(self, value):
        super().__init__(value)
        self.value = value

    def __str__(self):
        return repr(self.value)


class CentroidFail(Exception):
    """
    Some number was out of range
    """

    def __init__(self, value):
        super().__init__(value)
        self.value = value

    def __str__(self):
        return repr(self.value)
