# Licensed under a 3-clause BSD style license - see LICENSE.rst

import glob
import os
import re

from astropy import log
from astropy.io import fits
import numpy as np

from sofia_redux.toolkit.utilities.fits import goodfile, gethdul, hdinsert
from sofia_redux.spectroscopy.smoothres import smoothres

import sofia_redux.instruments.forcast as drip

__all__ = ['clear_atran_cache', 'get_atran_from_cache',
           'store_atran_in_cache', 'get_atran']

__atran_cache = {}


def clear_atran_cache():
    """
    Clear all data from the atran cache.
    """
    global __atran_cache
    __atran_cache = {}


def get_atran_from_cache(atranfile, resolution):
    """
    Retrieves atmospheric transmission data from the atran cache.

    Checks to see if the file still exists, can be read, and has not
    been altered since last time.  If the file has changed, it will be
    deleted from the cache so that it can be re-read.

    Parameters
    ----------
    atranfile : str
        File path to the atran file
    resolution : float
        Spectral resolution used for smoothing.

    Returns
    -------
    filename,  wv_scale
        filename : str
            Used to update ATRNFILE in FITS headers.
        wv_scale : float
            Used to update WVSCALE in FITS headers and apply WV correction.
        wave : numpy.ndarray
            (nwave,) array of wavelengths.
        unsmoothed : numpy.ndarray
            (nwave,) array containing the atran data from file
        smoothed : numpy.ndarray
            (nwave,) array containing the smoothed atran data
    """
    global __atran_cache

    key = atranfile, int(resolution)

    if key not in __atran_cache:
        return

    if not goodfile(atranfile):
        try:
            del __atran_cache[key]
        except KeyError:   # pragma: no cover
            # could happen in race conditions, working in parallel
            pass
        return

    modtime = str(os.path.getmtime(atranfile))
    if modtime not in __atran_cache.get(key, {}):
        return

    log.debug("Retrieving ATRAN data from cache (%s, resolution %s) " % key)
    return __atran_cache.get(key, {}).get(modtime)


def store_atran_in_cache(atranfile, resolution, filename, wv_scale, wave,
                         unsmoothed, smoothed):
    """
    Store atran data in the atran cache.

    Parameters
    ----------
    atranfile : str
        File path to the atran file
    resolution : float
        Spectral resolution used for smoothing.
    filename : str
        Used to update ATRNFILE in FITS headers.
    wv_scale : float
        Used to update WVSCALE in FITS headers and apply WV correction.
    wave : numpy.ndarray
        (nwave,) array of wavelengths.
    unsmoothed : numpy.ndarray
        (nwave,) array containing the atran data from file
    smoothed : numpy.ndarray
        (nwave,) array containing the smoothed atran data

    """
    global __atran_cache
    key = atranfile, int(resolution)
    log.debug("Storing ATRAN data in cache (%s, resolution %s)" % key)
    __atran_cache[key] = {}
    modtime = str(os.path.getmtime(atranfile))
    __atran_cache[key][modtime] = (
        filename, wv_scale, wave, unsmoothed, smoothed)


def get_atran(header, resolution, filename=None,
              get_unsmoothed=False, use_wv=False, atran_dir=None,
              wmin=4, wmax=50):
    """
    Retrieve reference atmospheric transmission data.

    ATRAN files in the data/atran_files directory should be named
    according to the altitude, ZA, and wavelengths for which they
    were generated, as:

        atran_[alt]K_[za]deg_[wmin]-[wmax]mum.fits

    For example, the file generated for altitude of 41,000 feet,
    ZA of 45 degrees, and wavelengths between 40 and 300 microns
    should be named:

        atran_41K_45deg_40-300mum.fits

    If use_wv is set, files named for the precipitable water vapor
    values for which they were generated will be used instead, as:

        atran_[alt]K_[za]deg_[wv]pwv_[wmin]-[wmax]mum.fits

    The procedure is:

        1. Identify ATRAN file by ZA, Altitude, and WV , unless override is
           provided.
        2. Read ATRAN data from file and smooth to expected spectral
           resolution.
        3. Return transmission array.

    Parameters
    ----------
    header : astropy.io.fits.header.Header
        ATRNFILE keyword is written to the provided FITS header,
        containing the name of the ATRAN file used.
    resolution : float
        Spectral resolution to which ATRAN data should be smoothed.
    filename : str, optional
        Atmospheric transmission file to be used.  If not provided,
        a default file will be retrieved from the data/grism/atran
        directory.  The file with the closest matching ZA and
        Altitude to the input data will be used.  If override file
        is provided, it should be a FITS image file containing
        wavelength and transmission data without smoothing.
    get_unsmoothed : bool, optional
        If True, return the unsmoothed atran data with original
        wavelength array in addition to the smoothed array.
    atran_dir : str, optional
        Path to a directory containing ATRAN reference FITS files.
        If not provided, the default set of files packaged with the
        pipeline will be used.
    use_wv : bool, optional
        If set, water vapor values from the header will be used
        to select the correct ATRAN file.
    wmin : int, optional
        Wavelength minimum to match ATRAN file names.
    wmax : int, optional
        Wavelength maximum to match ATRAN file names.

    Returns
    -------
    atran : numpy.ndarray
        A (2, nw) array containing wavelengths and transmission data.
    unsmoothed : numpy.ndarray, optional
        A (2, nw) array containing wavelengths and unsmoothed
        transmission data, returned only if get_unsmoothed is set.
    """
    wv_scale = 1.0
    if filename is not None:
        if not goodfile(filename, verbose=True):
            log.warning('File {} not found; '
                        'retrieving default'.format(filename))
            filename = None
    if filename is None:
        if not isinstance(header, fits.header.Header):
            log.error("Invalid header")
            return
        za_start = float(header.get('ZA_START', 0))
        za_end = float(header.get('ZA_END', 0))
        if za_start > 0 >= za_end:
            za = za_start
        elif za_end > 0 >= za_start:
            za = za_end
        else:
            za = 0.5 * (za_start + za_end)

        alt_start = float(header.get('ALTI_STA', 0))
        alt_end = float(header.get('ALTI_END', 0))
        if alt_start > 0 >= alt_end:
            alt = alt_start
        elif alt_end > 0 >= alt_start:
            alt = alt_end
        else:
            alt = 0.5 * (alt_start + alt_end)
        alt /= 1000

        wv_obs = float(header.get('WVZ_OBS', 0))
        if wv_obs > 0:
            wv = wv_obs
        else:
            wv_start = float(header.get('WVZ_STA', 0))
            wv_end = float(header.get('WVZ_END', 0))
            if wv_start > 0 >= wv_end:
                wv = wv_start
            elif wv_end > 0 >= wv_start:
                wv = wv_end
            else:
                wv = 0.5 * (wv_start + wv_end)
        if use_wv and wv < 2:
            # wv values aren't really used for forcast -- just pick one
            log.debug('Bad WV value: %f' % wv)
            log.debug('Using default value 6.0 um.')
            wv = 6.0

        log.debug('Alt, ZA, WV: %.2f %.2f %.2f' % (alt, za, wv))

        if atran_dir is not None:
            if not os.path.isdir(str(atran_dir)):
                log.warning('Cannot find ATRAN directory: %s' % atran_dir)
                log.warning('Using default ATRAN set.')
                atran_dir = None
        if atran_dir is None:
            atran_dir = os.path.join(os.path.dirname(drip.__file__),
                                     'data', 'grism', 'atran')

        atran_files = glob.glob(os.path.join(atran_dir, 'atran*fits'))
        regex1 = re.compile(rf'^atran_([0-9]+)K_([0-9]+)deg_'
                            rf'{wmin}-{wmax}mum\.fits$')
        regex2 = re.compile(rf'^atran_([0-9]+)K_([0-9]+)deg_'
                            rf'([0-9]+)pwv_{wmin}-{wmax}mum\.fits$')

        filename, minval = None, None
        wvfilename, wvminval = None, None
        for f in atran_files:
            match = regex2.match(os.path.basename(f))
            if match is not None:
                val = abs(float(match.group(1)) - alt) / float(match.group(1))
                val += abs(float(match.group(2)) - za) / float(match.group(2))
                val += abs(float(match.group(3)) - wv) / float(match.group(3))
                if wvminval is None or val < wvminval:
                    wvminval, wvfilename = val, f
            else:
                match = regex1.match(os.path.basename(f))
                if match is not None:
                    val = abs(float(match.group(1)) - alt) \
                        / float(match.group(1))
                    val += abs(float(match.group(2)) - za) \
                        / float(match.group(2))
                    if minval is None or val < minval:
                        minval, filename = val, f

        if use_wv and isinstance(wvfilename, str):
            log.debug('Using nearest Alt/ZA/WV')
            filename = wvfilename
        elif use_wv:
            # placeholder for scaling standard file by WV value
            wv_scale = 1.0
            log.debug('Using nearest Alt/ZA and scale factor %f' % wv_scale)
        else:
            log.debug('Using nearest Alt/ZA')

    if not isinstance(filename, str):
        log.debug("No ATRAN file found")
        return
    log.debug("Using ATRAN file %s" % filename)

    # Read the atran data from cache if possible
    atrandata = get_atran_from_cache(filename, resolution)
    if atrandata is not None:
        atranfile, wv_scale, wave, unsmoothed, smoothed = atrandata
    else:
        hdul = gethdul(filename, verbose=True)
        if hdul is None or hdul[0].data is None:
            log.error("Invalid data in ATRAN file %s" % filename)
            return
        data = hdul[0].data
        hdul.close()
        data[1] *= wv_scale
        wave = data[0]
        unsmoothed = data[1]
        smoothed = smoothres(data[0], data[1], resolution)
        atranfile = os.path.basename(filename)
        store_atran_in_cache(filename, resolution, atranfile, wv_scale,
                             data[0], data[1], smoothed)

    hdinsert(header, 'WVSCALE', wv_scale)
    hdinsert(header, 'ATRNFILE', atranfile)

    if not get_unsmoothed:
        return np.vstack((wave, smoothed))
    else:
        return np.vstack((wave, smoothed)), np.vstack((wave, unsmoothed))
