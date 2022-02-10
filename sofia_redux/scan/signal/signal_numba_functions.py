# Licensed under a 3-clause BSD style license - see LICENSE.rst

import numba as nb
import numpy as np

from sofia_redux.scan.utilities import numba_functions

nb.config.THREADING_LAYER = 'threadsafe'

__all__ = ['get_signal_variance', 'get_ml_correlated',
           'get_robust_correlated', 'resync_gains', 'apply_gain_increments',
           'calculate_filtering', 'differentiate_signal',
           'differentiate_weighted_signal', 'integrate_signal',
           'integrate_weighted_signal', 'add_drifts', 'level',
           'remove_drifts', 'get_covariance', 'get_ml_gain_increment',
           'get_robust_gain_increment', 'synchronize_gains',
           'prepare_frame_temp_fields']


@nb.jit(cache=True, nogil=False, Parallel=False, fastmath=False)
def get_signal_variance(values, weights=None):
    """
    Return the signal variance.

    The signal variance is returned as:

    v = sum(w * x^2) / sum(w)

    where x are the signal values and w are the signal weights.

    Parameters
    ----------
    values : numpy.ndarray (float)
        The signal values of shape (n_signal,).
    weights : numpy.ndarray (float), optional
        The signal weights of shape (n_signal,).

    Returns
    -------
    variance : float
    """
    v_sum = 0.0
    w_sum = 0.0
    do_weights = weights is not None
    for i in range(values.size):
        v = values[i]
        if np.isnan(v):
            continue
        if do_weights:
            w = weights[i]
        else:
            w = 1.0
        v_sum += v * v * w
        w_sum += w

    if w_sum == 0:
        return 0.0
    else:
        return v_sum / w_sum


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def get_ml_correlated(frame_data, frame_weights, frame_valid, channel_indices,
                      channel_wg, channel_wg2, sample_flags, resolution):
    """
    Derive the maximum-likelihood gain increments and weights.

    The return values are:

    sum(fw * cw * gain * data) / sum(fw * cw * gain^2)

    where fw are the relative frame weights and cw are the channel weights.

    Parameters
    ----------
    frame_data : numpy.ndarray (float)
        The frame data of shape (n_frames, all_channels).
    frame_weights : numpy.ndarray (float)
        The array of frame relative weights of shape (n_frames,).
    frame_valid : numpy.ndarray (bool)
        A boolean mask of shape (n_frames,) where `False` excludes a
        frame from any calculations or updates.  Should include both
        invalid frames and modeling flags.
    channel_indices : numpy.ndarray (int)
        An array of shape (n_channels,) mapping n_channels to all_channels.
    channel_wg : numpy.ndarray (float)
        An array of shape (n_channels,) containing the channel gains
        multiplied by the channel weights.
    channel_wg2 : numpy.ndarray (float)
        An array of shape (n_channels,) containing the channel gains^2
        multiplied by the channel weights.
    sample_flags : numpy.ndarray (int)
        An array of sample flags of shape (n_frames, all_channels) where
        any non-zero value excludes a data sample from being included in
        calculations.
    resolution : int
        The signal resolution (number of frames).

    Returns
    -------
    gain_increments, gain_increment_weights : numpy.ndarray, numpy.ndarray
        The gain increments and weights, both float arrays of shape
        (n_frames // resolution,) that should be applied to the signal.
    """
    n_frames = frame_data.shape[0]
    if resolution < 1:
        resolution = 1
    n_blocks = numba_functions.roundup_ratio(n_frames, resolution)
    gain_increments = np.empty(n_blocks, dtype=nb.float64)
    gain_increment_weights = np.empty(n_blocks, dtype=nb.float64)

    for block in range(n_blocks):
        start_frame = block * resolution
        end_frame = start_frame + resolution
        signal_index = block
        sum_wv = 0.0
        sum_w = 0.0
        for frame_index in range(start_frame, end_frame):
            if not frame_valid[frame_index]:
                continue
            fw = frame_weights[frame_index]
            if fw == 0:
                continue
            for i, channel_index in enumerate(channel_indices):
                if sample_flags[frame_index, channel_index] != 0:
                    continue
                sum_wv += (fw * channel_wg[i] *
                           frame_data[frame_index, channel_index])
                sum_w += fw * channel_wg2[i]
        if sum_w == 0:
            gain_increments[signal_index] = 0.0
            gain_increment_weights[signal_index] = 0.0
        else:
            gain_increments[signal_index] = sum_wv / sum_w
            gain_increment_weights[signal_index] = sum_w

    return gain_increments, gain_increment_weights


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def get_robust_correlated(frame_data, frame_weights, frame_valid,
                          channel_indices, channel_g, channel_wg2,
                          sample_flags, resolution, max_dependence=0.25):
    """
    Derive the robust gain increment and weights.

    The return values are:

    median(data / gain)

    where the median is weighted by:

    relative_frame_weight * channel_weight * gain^2

    Parameters
    ----------
    frame_data : numpy.ndarray (float)
        The frame data of shape (n_frames, all_channels).
    frame_weights : numpy.ndarray (float)
        The array of frame relative weights of shape (n_frames,).
    frame_valid : numpy.ndarray (bool)
        A boolean mask of shape (n_frames,) where `False` excludes a
        frame from any calculations or updates.  Should include both
        invalid frames and modeling flags.
    channel_indices : numpy.ndarray (int)
        An array of shape (n_channels,) mapping n_channels to all_channels.
    channel_g : numpy.ndarray (float)
        An array of shape (n_channels,) containing the channel gains.
    channel_wg2 : numpy.ndarray (float)
        An array of shape (n_channels,) containing the channel gains^2
        multiplied by the channel weights.
    sample_flags : numpy.ndarray (int)
        An array of sample flags of shape (n_frames, all_channels) where
        any non-zero value excludes a data sample from being included in
        calculations.
    resolution : int
        The signal resolution (number of frames).
    max_dependence : float, optional
        The maximum dependence of a single datum before switching to weighted
        mean.

    Returns
    -------
    mean, mean_weight : float, float
        The mean as described above and weight (denominator).
    """
    n_frames = frame_data.shape[0]
    if resolution < 1:
        resolution = 1
    n_blocks = numba_functions.roundup_ratio(n_frames, resolution)
    signal_values = np.empty(n_blocks, dtype=nb.float64)
    signal_weights = np.empty(n_blocks, dtype=nb.float64)
    buffer_size = resolution * channel_indices.size
    buffer_values = np.empty(buffer_size, dtype=nb.float64)
    buffer_weights = np.empty(buffer_size, dtype=nb.float64)

    for block in range(n_blocks):
        start_frame = block * resolution
        end_frame = start_frame + resolution
        signal_index = block
        n = 0
        for frame_index in range(start_frame, end_frame):
            if not frame_valid[frame_index]:
                continue
            fw = frame_weights[frame_index]
            if fw == 0:
                continue
            for i, channel_index in enumerate(channel_indices):
                gain = channel_g[i]
                if gain == 0:
                    continue
                if sample_flags[frame_index, channel_index] != 0:
                    continue
                value = frame_data[frame_index, channel_index] / gain
                weight = fw * channel_wg2[i]
                buffer_values[n] = value
                buffer_weights[n] = weight
                n += 1

        if n == 0:
            signal_values[signal_index] = 0.0
            signal_weights[signal_index] = 0.0
        else:
            value, weight = numba_functions.smart_median_1d(
                buffer_values[:n], buffer_weights[:n],
                max_dependence=max_dependence)
            signal_values[signal_index] = value
            signal_weights[signal_index] = weight

    return signal_values, signal_weights


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def resync_gains(frame_data, signal_values, resolution, delta_gains,
                 channel_indices, frame_valid):
    """
    Resync gains for a given block of frames.

    Removes the previous gain increment correction from frame data.  All frame
    data values are decremented by

    signal[frame_index//resolution] * channel_gain_delta.

    Parameters
    ----------
    frame_data : numpy.ndarray (float)
        The frame data values of shape (n_frames, all_channels).  The frame
        data values will be updated in-place.
    signal_values : numpy.ndarray (float)
        The signal values of shape (n_signal,).
    resolution : int
        The signal resolution.
    delta_gains : numpy.ndarray (float)
        The channel gain deltas of shape (n_channels,).
    channel_indices : numpy.ndarray (int)
        An array mapping n_channels onto all channels of shape
        (n_channels,).
    frame_valid : numpy.ndarray (bool)
        A boolean mask of shape (n_frames,) where `False` excludes a frame
        from all calculations and updates.

    Returns
    -------
    None
    """
    n_frames = frame_data.shape[0]
    if resolution < 1:
        resolution = 1
    n_signal = numba_functions.roundup_ratio(n_frames, resolution)
    for signal_index in range(n_signal):
        start_frame = signal_index * resolution
        end_frame = start_frame + resolution
        c = signal_values[signal_index]
        if c == 0:
            continue
        for frame in range(start_frame, end_frame):
            if not frame_valid[frame]:
                continue
            for i, channel in enumerate(channel_indices):
                dg = delta_gains[i]
                if dg == 0:
                    continue
                frame_data[frame, channel] -= dg * c


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def apply_gain_increments(frame_data, frame_weight, frame_valid,
                          modeling_frames, frame_dependents, channel_g,
                          channel_fwg2, channel_indices, channel_dependents,
                          sample_flags, signal_values, signal_weights,
                          resolution, increment, increment_weight):
    """
    Apply the gain increments to frame data and signals.

    Updates the frame data, frame and channel dependents, and signal values and
    weights by the given correlated gain increments.

    Parameters
    ----------
    frame_data : numpy.ndarray (float)
        The frame data of shape (n_frames, all_channels).  Will be updated
        in-place.
    frame_weight : numpy.ndarray (float)
        The relative frame weight of shape (n_frames,).
    frame_valid : numpy.ndarray (bool)
        A boolean mask where `False` excludes a given frame from all processing.
    modeling_frames : numpy.ndarray (bool)
        A boolean mask where `True` indicates that a given frame is used for
        modeling.  While frame data values will still be updated for modeling
        frames, they will not be used to update dependents.
    frame_dependents : numpy.ndarray (float)
        The frame dependents of shape (n_frames,).  Will be incremented by
        channel_{fwg2} * frame_{w} / increment_weight.
    channel_g : numpy.ndarray (float)
        The channel mode gains of shape (n_channels,) where n_channels are the
        number of channels in the signal mode.
    channel_fwg2 : numpy.ndarray (float)
        The product of channel (filtering * weight * gain^2) of shape
        (n_channels,).
    channel_indices : numpy.ndarray (int)
        The channel indices in the mode channel group of shape (n_channels,)
        mapping each channel onto (all_channels,) for frame data.
    channel_dependents : numpy.ndarray (float)
        The channel dependents of shape (all_channels,).  Will be incremented
        by channel_{fwg2} * frame_{w} / increment_weight.
    sample_flags : numpy.ndarray (int)
        The frame/channel sample flags of shape (n_frames, all_channels).  Non-
        zero sample flags will not add to frame of channel dependents.
    signal_values : numpy.ndarray (float)
        The signal values of the correlated signal of shape (n_signal,).  Will
        be incremented in-place by the increment values.
    signal_weights : numpy.ndarray (float)
        The signal weights of the correlated signal of shape (n_signal,).  Will
        be updated in-place to the increment weights.
    resolution : int
        The resolution (in frames) of the correlated signal.
    increment : numpy.ndarray (float)
        The signal increment values of shape (n_signal,).
    increment_weight : numpy.ndarray (float)
        The signal increment weight value of shape (n_signal,).

    Returns
    -------
    None
    """
    n_frames = frame_data.shape[0]
    if resolution < 1:
        resolution = 1
    n_signal = numba_functions.roundup_ratio(n_frames, resolution)

    for signal_index in range(n_signal):
        start_frame = signal_index * resolution
        end_frame = start_frame + resolution
        dw = increment_weight[signal_index]
        if dw <= 0:
            continue
        dc = increment[signal_index]

        for frame in range(start_frame, end_frame):
            if not frame_valid[frame]:
                continue

            # Here the current gains carry the gain increment dG from the last
            # correlated signal removal
            for i, channel in enumerate(channel_indices):
                frame_data[frame, channel] -= channel_g[i] * dc

            if modeling_frames[frame]:
                continue

            fp_norm = frame_weight[frame] / dw
            if fp_norm == 0:
                continue

            for i, channel in enumerate(channel_indices):
                if sample_flags[frame, channel] != 0:
                    continue
                fwg2 = channel_fwg2[i]
                if fwg2 == 0:
                    continue
                dp = fp_norm * fwg2
                frame_dependents[frame] += dp
                channel_dependents[channel] += dp

        # Update the correlated signal model
        signal_values[signal_index] += dc
        signal_weights[signal_index] = dw


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def calculate_filtering(channel_indices, channel_dependents, overlaps,
                        channel_valid, n_parms, channel_source_filtering,
                        signal_source_filtering):
    """
    Calculate the new signal and channel source filtering.

    Parameters
    ----------
    channel_indices : numpy.ndarray (int)
        The channel indices for which all channel type data is applicable.
        This is an array of shape (n_channels,) which maps n_channels onto
        all_channels (used in channel_dependents).
    channel_dependents : numpy.ndarray (float)
        The channel dependent values of shape (all_channels,).
    overlaps : numpy.ndarray (float)
        The channel overlap values of shape (n_channels, n_channels) where
        overlaps[i, j] gives the overlap value between channel i and j and
        should therefore be triangularly symmetrical.
    channel_valid : numpy.ndarray (bool)
        A boolean mask of shape (n_channels,) where `False` excludes a given
        channel from being included in any derivations.
    n_parms : float
        The relative degrees of freedom for the signal.  Typically given by
        sum(weights > 0) * (1 - 1/n_drifts).
    channel_source_filtering : numpy.ndarray (float)
        The current channel source filtering of shape (n_channels,).
    signal_source_filtering : numpy.ndarray (float)
        The current signal source filtering of shape (n_channels,).

    Returns
    -------
    new_channel_source_filtering, new_signal_source_filtering : ndarray, ndarray
        The updated channel and signal source filtering, both of shape
        (n_channels,).
    """

    n_channels = channel_indices.size
    # phi = np.zeros(n_channels, dtype=nb.float64)

    new_signal_source_filtering = np.empty(n_channels, dtype=nb.float64)
    new_channel_source_filtering = np.empty(n_channels, dtype=nb.float64)

    # NOTE: Aborting triangular reduction due to floating point errors
    for i, channel_i in enumerate(channel_indices):
        if not channel_valid[i]:
            new_signal_source_filtering[i] = signal_source_filtering[i]
            new_channel_source_filtering[i] = channel_source_filtering[i]
            continue

        phi = channel_dependents[channel_i]
        for j, channel_j in enumerate(channel_indices):
            if not channel_valid[j]:
                continue
            elif i == j:
                continue
            overlap_value = overlaps[i, j]
            if overlap_value == 0:
                continue
            phi += overlap_value * channel_dependents[channel_j]

        if n_parms > 0:
            phi /= n_parms
        if phi > 1:
            phi = 1.0

        # undo the prior filtering correction
        sf = signal_source_filtering[i]
        cf = channel_source_filtering[i]
        if sf > 0:
            cf /= sf
        if np.isnan(cf):
            cf = 1.0

        # Calculate the new filtering gain correction and apply it
        sf = 1.0 - phi
        cf *= sf

        new_signal_source_filtering[i] = sf
        new_channel_source_filtering[i] = cf

    return new_channel_source_filtering, new_signal_source_filtering


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def differentiate_signal(values, dt=1.0):
    """
    Differentiate the signal values in-place.

    Parameters
    ----------
    values : numpy.ndarray (float)
        The signal values of shape (n_signal,).
    dt : float, optional
        The interval between signal samples.

    Returns
    -------
    None
    """
    nm1 = values.size - 1
    for i in range(nm1):
        values[i] = (values[i + 1] - values[i]) / dt
    # The last value is based on the last difference.
    values[-1] = values[-2]
    for i in range(nm1, 0, -1):
        values[i] = 0.5 * (values[i] + values[i - 1])


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def differentiate_weighted_signal(values, weights, dt=1.0):
    """
    Differentiate signal values and weights in-place.

    Parameters
    ----------
    values : numpy.ndarray (float)
        The signal values of shape (n_signal,).
    weights : numpy.ndarray (float)
        The signal weights of shape (n_signal,)
    dt : float, optional
        The interval between signal samples.

    Returns
    -------
    None
    """
    n = values.size
    dt2 = dt * dt
    for i in range(n - 1):
        v1 = values[i]
        v2 = values[i + 1]
        w1 = weights[i]
        w2 = weights[i + 1]
        w = w1 * w2
        if w != 0:
            w /= (w1 + w2) * dt2
        weights[i] = w
        values[i] = (v2 - v1) / dt

    # The last value is based on the last difference
    values[-1] = values[-2]
    weights[-1] = weights[-2]

    # v[n] = (f'[n+0.5] + f'[n-0.5]) = v[n] + v[n-1]
    for i in range(n - 1, 0, -1):  # only goes down to 1
        v1 = values[i]
        v2 = values[i - 1]
        w1 = weights[i]
        w2 = weights[i - 1]
        v = (w1 * v1) + (w2 * v2)
        w = w1 + w2
        if w > 0:
            v /= w
        values[i] = v
        weights[i] = w


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def integrate_signal(values, dt=1.0):
    """
    Integrate signal values in-place using the trapezoid rule.

    Parameters
    ----------
    values : numpy.ndarray of float
        1-D array containing values to integrate.
    dt : float, optional
        Spacing between samples.

    Returns
    -------
    None
    """
    i_val = 0.0
    half_last = 0.0

    for i in range(values.size):
        # Calculate next half increment of h/2 * value[i]
        half_next = 0.5 * values[i]

        # Add half increments from below and above
        i_val += half_last
        i_val += half_next

        values[i] = i_val * dt
        half_last = half_next


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def integrate_weighted_signal(values, weights, dt=1.0):
    """
    Integrate signal values and weights in-place using the trapezoid rule.

    Not sure if the weight calculation on this is accurate.  It's definitely
    not in the original CRUSH.

    Parameters
    ----------
    values : numpy.ndarray (float)
        The signal values to integrate in-place of shape (n_signal,).
    weights : numpy.ndarray (float)
        The signal weights to integrate in-place of shape (n_signal,).
    dt : float, optional
        The spacing between sample values.

    Returns
    -------
    None
    """
    dt2 = dt * dt
    idt = 1.0 / dt
    integral = 0.0
    v_last = 0.0
    w_last = 0.0
    half_dt = 0.5 * dt
    half_dt2 = half_dt * half_dt

    for i in range(values.size):
        # Calculate the next half increment of h/2 * f[t]
        v_next = values[i] * half_dt
        w_next = weights[i] / half_dt2

        w = w_last * w_next
        if w > 0:
            w /= w_last + w_next
        else:
            w = 0.0

        v = v_last + v_next
        integral += v
        values[i] = integral * idt
        weights[i] = w * dt2
        w_last = w_next
        v_last = v_next


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def add_drifts(signal_values, drifts, drift_length):
    """
    Add drifts to the signal values.

    Parameters
    ----------
    signal_values : numpy.ndarray (float)
        The signal values of shape (n_signal,).
    drifts : numpy.ndarray (float)
        The signal drifts of shape (n_drifts,).
    drift_length : int
        The number of signal values in each drift.

    Returns
    -------
    None
    """
    n_signal = signal_values.size
    n_drifts = drifts.size
    for drift in range(n_drifts):
        drift_value = drifts[drift]
        if drift_value == 0:
            continue
        start_signal_index = drift * drift_length
        end_signal_index = start_signal_index + drift_length
        if end_signal_index > n_signal:
            end_signal_index = n_signal

        for signal_index in range(start_signal_index, end_signal_index):
            signal_values[signal_index] += drift_value


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def level(values, start_frame, end_frame, resolution,
          weights=None, robust=False):
    """
    Remove and return the average value between a start and end frame.

    Parameters
    ----------
    values : numpy.ndarray (float)
        The signal values of length (n_signal,).
    start_frame : int
        The starting frame (inclusive).
    end_frame : int
        The end frame (non-inclusive).
    resolution : int
        The frame resolution of the signal (number of frames per signal
        measurement).
    weights : numpy.ndarray (float), optional
        The signal weights of shape (n_signal,).
    robust : bool, optional
        If `True`, remove the median value, otherwise remove the mean.

    Returns
    -------
    levelled_value : float
        The value subtracted from values.
    """
    start_signal_index = int(start_frame) // resolution
    end_signal_index = numba_functions.roundup_ratio(end_frame, resolution)
    x = values[start_signal_index:end_signal_index]
    if weights is not None:
        w = weights[start_signal_index:end_signal_index]
    else:
        w = np.ones(x.size, dtype=nb.float64)
    if robust:
        center, _ = numba_functions.smart_median_1d(values=x, weights=w)
    else:
        center, _ = numba_functions.mean(values=x, weights=w)

    # Remove the mean value in-place
    for signal_index in range(start_signal_index, end_signal_index):
        values[signal_index] -= center
    return center


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def remove_drifts(signal_values, drifts, n_frames, resolution, integration_size,
                  signal_weights=None, robust=False):
    """
    Remove drifts (average signal levels in a frame block) from the signal.

    Parameters
    ----------
    signal_values : numpy.ndarray (float)
        The signal values of shape (n_signal,)
    drifts : numpy.ndarray (float)
        The drift values.  These will be updated by incrementing the given
        values by the average signal value removed in each drift.  The number
        of drifts is also determined by the size of this array (n_drifts,).
    n_frames : int
        The number of frames in each drift.
    resolution : int
        The number of frames applicable to each signal value.
    integration_size : int
        The total number of frames in the integration belonging to the signal.
    signal_weights : numpy.ndarray (float), optional
        The optional signal weights.  If supplied, these will be used to
        determine a weighted mean/median values for each drift.
    robust : bool, optional
        If `True`, determine average signal values using the median value.
        Otherwise, uses the mean value.

    Returns
    -------
    None
    """

    for drift_index in range(drifts.size):
        start_frame = drift_index * n_frames
        end_frame = start_frame + n_frames
        if end_frame > integration_size:
            end_frame = integration_size

        center_value = level(
            values=signal_values,
            start_frame=start_frame,
            end_frame=end_frame,
            resolution=resolution,
            weights=signal_weights,
            robust=robust)

        drifts[drift_index] += center_value


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=True)
def get_covariance(signal_values, frame_data, frame_valid,
                   channel_indices, channel_weights, sample_flags):
    """
    Return the signal covariance for samples in frames/channel.

    The signal covariance is given as:

    C = sum_{channels}(xs * xs) / sum_{channels}(ss * xx)

    where xs = sum_{frames}(w * x * s), ss = sum_{frames}(w * s * s), and
    xx = sum_{frames}(w * x * x).  Here w is the channel weight, s is the signal
    value for each frame, and x are the frame data values.

    Parameters
    ----------
    signal_values : numpy.ndarray (float)
        The signal values of shape (n_frames,).
    frame_data : numpy.ndarray (float)
        The frame data of shape (n_frames, all_channels).
    frame_valid : numpy.ndarray (bool)
        A boolean mask of shape (n_frames,) where `False` excludes any given
        frame from processing.
    channel_indices : numpy.ndarray (int)
        The channel indices to include in the covariance calculation of shape
        (n_channels,).  Should map n_channels -> all_channels for frame and
        sample data.
    channel_weights : numpy.ndarray (float)
        The channel weights of shape (n_channels,).
    sample_flags : numpy.ndarray (int)
        The sample flags of shape (n_frames, n_channels).  Any non-zero value
        will not be included in the covariance calculation.

    Returns
    -------
    covariance : float
    """

    n_channels = channel_indices.size
    sum_xs = np.zeros(n_channels, dtype=nb.float64)
    sum_x2 = np.zeros(n_channels, dtype=nb.float64)
    sum_s2 = np.zeros(n_channels, dtype=nb.float64)

    for frame in range(frame_data.shape[0]):
        if not frame_valid[frame]:
            continue
        s = signal_values[frame]
        if np.isnan(s):
            continue
        for i, channel in enumerate(channel_indices):
            w = channel_weights[i]
            if w == 0:
                continue
            if sample_flags[frame, channel] != 0:
                continue
            x = frame_data[frame, channel]
            sum_x2[i] += w * x * x
            sum_xs[i] += w * x * s
            sum_s2[i] += w * s * s

    c2 = 0.0
    for i in range(n_channels):
        xs = sum_xs[i]
        if xs > 0:
            c2 += (xs * xs) / (sum_x2[i] * sum_s2[i])

    return np.sqrt(c2)


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def get_ml_gain_increment(frame_data, signal_wc, signal_wc2, sample_flags,
                          channel_indices, valid_frames):
    """
    Return the maximum-likelihood gain increment.

    The ML gain increment for any given channel is given as:

    dC = sum_{frames}(w * x * s) / dW

    where w are the frame relative weights, x are the frame data for the given
    channels, and s are the signal values.  The weight dW is given as:

    dW = sum_{frames}(w * s * s)

    Parameters
    ----------
    frame_data : numpy.ndarray (float)
        The frame data array of shape (n_frames, all_channels).
    signal_wc : numpy.ndarray (float)
        The weighted signal values (frame_weight * signal value).
        An array of shape (n_frames,).
    signal_wc2 : numpy.ndarray (float)
        The weighted square signal values (frame_weight * signal_value^2).
        An array of shape (n_frames,).
    sample_flags : numpy.ndarray (int)
        The frame data sample flags of shape (n_frames, all_channels).  Any
        non-zero sample will not be included in calculations.
    channel_indices : numpy.ndarray (int)
        An array of shape (n_channels,) mapping the mode channel group
        indices to all_channels in the frame_data.
    valid_frames : numpy.ndarray (bool)
        A boolean mask of shape (n_frames,) where `False` excludes a frame
        from any calculations.

    Returns
    -------
    increment, increment_weight : numpy.ndarray, numpy.ndarray
        The increment values and weights of shape (n_channels,).
    """
    n_channels = channel_indices.size
    n_frames = frame_data.shape[0]
    increment = np.zeros(n_channels, dtype=nb.float64)
    increment_weight = np.zeros(n_channels, dtype=nb.float64)

    for frame_index in range(n_frames):
        if not valid_frames[frame_index]:
            continue
        wc = signal_wc[frame_index]
        if wc == 0:
            continue  # No need to increment for zero signal value/weight
        wc2 = signal_wc2[frame_index]

        for i, channel_index in enumerate(channel_indices):
            if sample_flags[frame_index, channel_index] != 0:
                continue
            increment[i] += wc * frame_data[frame_index, channel_index]
            increment_weight[i] += wc2

    for i in range(n_channels):
        w = increment_weight[i]
        if w > 0:
            increment[i] /= w

    return increment, increment_weight


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def get_robust_gain_increment(frame_data, signal_c, signal_wc2, sample_flags,
                              channel_indices, valid_frames):
    """
    Fast implementation of `get_robust_gain_increment`.

    Parameters
    ----------
    frame_data : numpy.ndarray (float)
        The frame data array of shape (n_frames, all_channels).
    signal_c : numpy.ndarray (float)
        The signal values of shape (n_frames,).
    signal_wc2 : numpy.ndarray (float)
        The weighted square signal values (frame_weight * signal_value^2).
        An array of shape (n_frames,).
    sample_flags : numpy.ndarray (int)
        The frame data sample flags of shape (n_frames, all_channels).  Any
        non-zero sample will not be included in calculations.
    channel_indices : numpy.ndarray (int)
        An array of shape (n_channels,) mapping the mode channel group
        indices to all_channels in the frame_data.
    valid_frames : numpy.ndarray (bool)
        A boolean mask of shape (n_frames,) where `False` excludes a frame
        from any calculations.

    Returns
    -------
    increment, increment_weight : numpy.ndarray, numpy.ndarray
        The increment values and weights of shape (n_channels,).
    """
    n_channels = channel_indices.size
    n_frames = frame_data.shape[0]
    temp_data = np.empty(n_frames, dtype=nb.float64)
    temp_weight = np.empty(n_frames, dtype=nb.float64)
    increment = np.empty(n_channels, dtype=nb.float64)
    increment_weight = np.empty(n_channels, dtype=nb.float64)
    for i, channel in enumerate(channel_indices):
        n = 0
        for frame in range(n_frames):
            if not valid_frames[frame]:
                continue
            elif signal_wc2[frame] <= 0:
                continue
            elif sample_flags[frame, channel] != 0:
                continue

            temp_data[n] = frame_data[frame, channel] / signal_c[frame]
            temp_weight[n] = signal_wc2[frame]
            n += 1

        if n == 0:
            increment[i] = 0.0
            increment_weight[i] = 0.0
        else:
            mean, mean_w = numba_functions.smart_median_1d(
                temp_data[:n], temp_weight[:n], max_dependence=0.25)
            increment[i] = mean
            increment_weight[i] = mean_w

    return increment, increment_weight


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def synchronize_gains(frame_data, sample_flags, frame_valid,
                      modeling_frames, channel_indices, delta_gains,
                      frame_wc2, channel_wc2, signal_values,
                      frame_parms, channel_parms):
    """
    Fast implementation of `synchronize_gains` method.

    Parameters
    ----------
    frame_data : numpy.ndarray (float)
        The frame data of shape (n_frames, all_channels).  Will be updated
        in-place.
    sample_flags : numpy.ndarray (int)
        Sample flags of shape (n_frames, all_channels) where any non-zero
        value excludes a sample (frame data) from certain calculations.
        In this case, frame data will always be updated, but dependents
        will not for a non-zero sample flag.
    frame_valid : numpy.ndarray (bool)
        A boolean mask of shape (n_frames,) where `False` excludes a frame
        from all calculations.
    modeling_frames : numpy.ndarray (bool)
        A boolean mask of shape (n_frames,) where `True` indicates that a
        frame is a modeling frame for which dependents should not be
        updated.
    channel_indices : numpy.ndarray (int)
        The indices of the channel group for the signal mode.  Used to map
        n_channels onto all_channels.
    delta_gains : numpy.ndarray (float)
        The change in the gains of shape (n_channels,).  For zero deltas,
        no change needs to be made.
    frame_wc2 : numpy.ndarray (float)
        The frame signal gain weights of shape (n_frames,).
    channel_wc2 : numpy.ndarray (float)
        The channel signal gain weights of shape (n_channels,).
    signal_values : numpy.ndarray (float)
        The frame signal gain values of shape (n_frames,).
    frame_parms : numpy.ndarray (float)
        The frame dependents of shape (n_frames,).  Updated in-place.
    channel_parms : numpy.ndarray (float)
        The channel dependents of shape (all_channels,).  Updated in-place

    Returns
    -------
    None
    """
    for frame in range(frame_data.shape[0]):
        if not frame_valid[frame]:
            continue
        for i, channel in enumerate(channel_indices):

            # Resync gains
            c_wc2 = channel_wc2[i]
            if c_wc2 <= 0:
                continue
            frame_data[frame, channel] -= delta_gains[i] * signal_values[frame]

            # Adjust frame parms.
            if modeling_frames[frame]:
                continue
            if sample_flags[frame, channel] != 0:
                continue
            frame_parms[frame] += frame_wc2[frame] / c_wc2

    # Account for the one gain parameter per channel minus the overall gain
    # renormalization.
    channel_dependence = 1.0 - (1.0 / channel_indices.size)
    for i, channel_index in enumerate(channel_indices):
        c_wc2 = channel_wc2[i]
        if c_wc2 > 0:
            channel_parms[channel_index] += channel_dependence


@nb.njit(cache=True, nogil=False, parallel=False, fastmath=False)
def prepare_frame_temp_fields(signal_values, frame_weights, frame_valid,
                              frame_modeling, frame_c, frame_wc, frame_wc2):
    for frame in range(frame_valid.size):
        if not frame_valid[frame]:
            continue
        c = signal_values[frame]
        if np.isnan(c):
            frame_c[frame] = frame_wc[frame] = frame_wc2[frame] = 0.0
            continue
        frame_c[frame] = c
        if frame_modeling[frame]:
            frame_wc[frame] = frame_wc2[frame] = 0.0
            continue
        w = frame_weights[frame]
        if w <= 0 or np.isnan(w):
            frame_wc[frame] = frame_wc2[frame] = 0.0
        wc = w * c
        frame_wc[frame] = wc
        frame_wc2[frame] = wc * c


