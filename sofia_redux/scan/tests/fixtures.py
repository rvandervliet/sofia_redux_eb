# Licensed under a 3-clause BSD style license - see LICENSE.rst

from astropy import units
from astropy.io import fits
import pytest

from sofia_redux.scan.custom.example.info.info import ExampleInfo
from sofia_redux.scan.custom.example.scan.scan import ExampleScan
from sofia_redux.scan.custom.hawc_plus.info.info import HawcPlusInfo
from sofia_redux.scan.custom.hawc_plus.scan.scan import HawcPlusScan
from sofia_redux.scan.custom.hawc_plus.simulation.simulation \
    import HawcPlusSimulation
from sofia_redux.scan.reduction.reduction import Reduction


# Example instrument simulation

@pytest.fixture
def scan_file(tmpdir):
    reduction = Reduction('example')
    fname = str(tmpdir.join('test.fits'))
    reduction.info.write_simulated_hdul(fname, fwhm=10 * units.arcsec)
    return fname


@pytest.fixture
def bad_file(tmpdir):
    hdul = fits.HDUList(fits.PrimaryHDU(data=[1, 2, 3]))
    fname = str(tmpdir.join('bad.fits'))
    hdul.writeto(fname, overwrite=True)
    hdul.close()
    return fname


@pytest.fixture
def initialized_scan():
    info = ExampleInfo()
    info.read_configuration()
    channels = info.get_channels_instance()
    scan = ExampleScan(channels)
    return scan


@pytest.fixture
def populated_scan(scan_file):
    info = ExampleInfo()
    info.read_configuration()
    channels = info.get_channels_instance()
    scan = channels.read_scan(scan_file)
    return scan


@pytest.fixture
def reduced_scan(scan_file, tmpdir):
    with tmpdir.as_cwd():
        reduction = Reduction('example')
        reduction.run(scan_file)
    return reduction.scans[0]


@pytest.fixture
def pointing_scan(scan_file, tmpdir):
    with tmpdir.as_cwd():
        reduction = Reduction('example')
        reduction.configuration.set_option('point', True)
        reduction.run(scan_file)
    return reduction.scans[0]


@pytest.fixture
def focal_pointing_scan(scan_file, tmpdir):
    with tmpdir.as_cwd():
        reduction = Reduction('example')
        reduction.configuration.set_option('point', True)
        reduction.configuration.set_option('focalplane', True)
        reduction.run(scan_file)
    return reduction.scans[0]


@pytest.fixture
def populated_integration(populated_scan):
    return populated_scan.integrations[0]


# HAWC+ simulation

@pytest.fixture
def hawc_scan_file(tmpdir):
    reduction = Reduction('hawc_plus')
    sim = HawcPlusSimulation(reduction.info)

    header_options = fits.Header()
    header_options['CHPNOISE'] = 3.0  # Chopper noise (arcsec)
    header_options['SRCAMP'] = 20.0  # NEFD estimate
    header_options['SRCS2N'] = 30.0  # source signal to noise
    header_options['OBSDEC'] = 7.406657  # declination (degree)
    header_options['OBSRA'] = 1.272684  # ra (hours)
    header_options['SPECTEL1'] = 'HAW_C'  # sets band
    header_options['SRCSIZE'] = 20  # source FWHM (arcsec)
    header_options['ALTI_STA'] = 41993.0
    header_options['ALTI_END'] = 41998.0
    header_options['LON_STA'] = -108.182373
    header_options['LAT_STA'] = 47.043457
    header_options['EXPTIME'] = 30.0  # scan length (seconds)
    header_options['DATE-OBS'] = '2016-12-14T06:41:30.450'

    hdul = sim.create_simulated_hdul(header_options=header_options)

    fname = str(tmpdir.join('test.fits'))
    hdul.writeto(fname, overwrite=True)
    hdul.close()
    return fname


@pytest.fixture
def hawc_chopscan_file(tmpdir):
    reduction = Reduction('hawc_plus')
    sim = HawcPlusSimulation(reduction.info)

    header_options = fits.Header()
    header_options['CHOPPING'] = True
    header_options['CHPNOISE'] = 3.0  # Chopper noise (arcsec)
    header_options['SRCAMP'] = 20.0  # NEFD estimate
    header_options['SRCS2N'] = 30.0  # source signal to noise
    header_options['OBSDEC'] = 7.406657  # declination (degree)
    header_options['OBSRA'] = 1.272684  # ra (hours)
    header_options['SPECTEL1'] = 'HAW_C'  # sets band
    header_options['SRCSIZE'] = 20  # source FWHM (arcsec)
    header_options['ALTI_STA'] = 41993.0
    header_options['ALTI_END'] = 41998.0
    header_options['LON_STA'] = -108.182373
    header_options['LAT_STA'] = 47.043457
    header_options['EXPTIME'] = 30.0  # scan length (seconds)
    header_options['DATE-OBS'] = '2016-12-14T06:41:30.450'

    hdul = sim.create_simulated_hdul(header_options=header_options)

    fname = str(tmpdir.join('test.fits'))
    hdul.writeto(fname, overwrite=True)
    hdul.close()
    return fname


@pytest.fixture
def initialized_hawc_scan():
    info = HawcPlusInfo()
    info.read_configuration()
    channels = info.get_channels_instance()
    scan = HawcPlusScan(channels)
    return scan


@pytest.fixture
def populated_hawc_scan(hawc_scan_file):
    info = HawcPlusInfo()
    info.read_configuration()
    channels = info.get_channels_instance()
    scan = channels.read_scan(hawc_scan_file)
    return scan


@pytest.fixture
def populated_hawc_chopscan(hawc_chopscan_file):
    info = HawcPlusInfo()
    info.read_configuration()
    channels = info.get_channels_instance()
    scan = channels.read_scan(hawc_chopscan_file)
    return scan


@pytest.fixture
def reduced_hawc_scan(hawc_scan_file, tmpdir):
    with tmpdir.as_cwd():
        reduction = Reduction('hawc_plus')
        reduction.run(hawc_scan_file, shift=0.0, blacklist='correlated.bias')
    return reduction.scans[0]


@pytest.fixture
def populated_hawc_integration(populated_hawc_scan):
    return populated_hawc_scan.integrations[0]


@pytest.fixture
def populated_hawc_chop_integration(populated_hawc_chopscan):
    return populated_hawc_chopscan.integrations[0]
