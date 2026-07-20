import numpy as np

def halman_filter(series: np.ndarray, window: int, n_sigmas: float = 2.5) -> np.ndarray:
    if window < 3 or window % 2 == 0:
        raise ValueError("window must be odd and >= 3")
    if len(series) < window:
        return series.copy()

    k = window // 2
    # Create sliding windows without copying data
    from numpy.lib.stride_tricks import sliding_window_view
    windows = sliding_window_view(series, window)
    
    # Median per window
    medians = np.median(windows, axis=1)
    
    # MAD per window
    devs = np.abs(windows - medians[:, np.newaxis])
    mads = np.median(devs, axis=1)
    
    # Pad medians & mads to match original length (reflect/edge behavior)
    pad = (k, k)
    med_pad = np.pad(medians, pad, mode='edge')
    mad_pad = np.pad(mads,    pad, mode='edge')
    
    threshold = n_sigmas * 1.4826 * mad_pad
    mask_outlier = np.abs(series - med_pad) > threshold
    
    filtered = np.where(mask_outlier, med_pad, series)
    return filtered.astype(series.dtype)