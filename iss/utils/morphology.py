import numpy as np
import scipy.signal
from scipy.ndimage.morphology import grey_dilation
from scipy.ndimage import convolve, correlate
import numbers
from . import errors
import cv2
from typing import Optional, Union, Tuple
from scipy.signal import oaconvolve
import jax.numpy as jnp
import jax


def ftrans2(b: np.ndarray, t: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Produces a 2D convolve kernel that corresponds to the 1D convolve kernel, `b`, using the transform, `t`.
    Copied from [MATLAB `ftrans2`](https://www.mathworks.com/help/images/ref/ftrans2.html).

    Args:
        b: `float [Q]`.
            1D convolve kernel.
        t: `float [M x N]`.
            Transform to make `b` a 2D convolve kernel.
            If `None`, McClellan transform used.

    Returns:
        `float [(M-1)*(Q-1)/2+1 x (N-1)*(Q-1)/2+1]`.
            2D convolve kernel.
    """
    if t is None:
        # McClellan transformation
        t = np.array([[1, 2, 1], [2, -4, 2], [1, 2, 1]]) / 8

    # Convert the 1-D convolve_2d b to SUM_n a(n) cos(wn) form
    n = int(round((len(b) - 1) / 2))
    b = b.reshape(-1, 1)
    b = np.rot90(np.fft.fftshift(np.rot90(b)))
    a = np.concatenate((b[:1], 2 * b[1:n + 1]))

    inset = np.floor((np.array(t.shape) - 1) / 2).astype(int)

    # Use Chebyshev polynomials to compute h
    p0 = 1
    p1 = t
    h = a[1] * p1
    rows = inset[0]
    cols = inset[1]
    h[rows, cols] += a[0] * p0
    for i in range(2, n + 1):
        p2 = 2 * scipy.signal.convolve2d(t, p1)
        rows = rows + inset[0]
        cols = cols + inset[1]
        p2[rows, cols] -= p0
        rows = inset[0] + np.arange(p1.shape[0])
        cols = (inset[1] + np.arange(p1.shape[1])).reshape(-1, 1)
        hh = h.copy()
        h = a[i] * p2
        h[rows, cols] += hh
        p0 = p1.copy()
        p1 = p2.copy()
    h = np.rot90(h)
    return h


def hanning_diff(r1: int, r2: int) -> np.ndarray:
    """
    Gets difference of two hanning window 2D convolve kernel.
    Central positive, outer negative with sum of `0`.

    Args:
        r1: radius in pixels of central positive hanning convolve kernel.
        r2: radius in pixels of outer negative hanning convolve kernel.

    Returns:
        `float [2*r2+1 x 2*r2+1]`.
            Difference of two hanning window 2D convolve kernel.
    """
    if not 0 <= r1 <= r2-1:
        raise errors.OutOfBoundsError("r1", r1, 0, r2-1)
    if not r1+1 <= r2 <= np.inf:
        raise errors.OutOfBoundsError("r2", r1+1, np.inf)
    h_outer = np.hanning(2 * r2 + 3)[1:-1]  # ignore zero values at first and last index
    h_outer = -h_outer / h_outer.sum()
    h_inner = np.hanning(2 * r1 + 3)[1:-1]
    h_inner = h_inner / h_inner.sum()
    h = h_outer.copy()
    h[r2 - r1:r2 + r1 + 1] += h_inner
    h = ftrans2(h)
    return h


def convolve_2d(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """
    Convolves `image` with `kernel`, padding by replicating border pixels.

    Args:
        image: `float [image_sz1 x image_sz2]`.
            Image to convolve.
        kernel: `float [kernel_sz1 x kernel_sz2]`.
            2D kernel

    Returns:
        `float [image_sz1 x image_sz2]`.
            `image` after being convolved with `kernel`.

    !!! note
        `np.flip` is used to give same result as `convn` with replicate padding in MATLAB.
    """
    return cv2.filter2D(image.astype(float), -1, np.flip(kernel), borderType=cv2.BORDER_REPLICATE)


def ensure_odd_kernel(kernel: np.ndarray, pad_location: str = 'start') -> np.ndarray:
    """
    This ensures all dimensions of `kernel` are odd by padding even dimensions with zeros.
    Replicates MATLAB way of dealing with even kernels.

    Args:
        kernel: `float [kernel_sz1 x kernel_sz2 x ... x kernel_szN]`.
        pad_location: One of the following, indicating where to pad with zeros -

            - `'start'` - Zeros at start of kernel.
            - `'end'` - Zeros at end of kernel.

    Returns:
        `float [odd_kernel_sz1 x odd_kernel_sz2 x ... x odd_kernel_szN]`.
            `kernel` padded with zeros so each dimension is odd.

    Example:
        If `pad_location` is `'start'` then `[[5,4];[3,1]]` becomes `[[0,0,0],[0,5,4],[0,3,1]]`.
    """
    even_dims = (np.mod(kernel.shape, 2) == 0).astype(int)
    if max(even_dims) == 1:
        if pad_location == 'start':
            pad_dims = [tuple(np.array([1, 0]) * val) for val in even_dims]
        elif pad_location == 'end':
            pad_dims = [tuple(np.array([0, 1]) * val) for val in even_dims]
        else:
            raise ValueError(f"pad_location has to be either 'start' or 'end' but value given was {pad_location}.")
        return np.pad(kernel, pad_dims, mode='constant')
    else:
        return kernel


def top_hat(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """
    Does tophat filtering of `image` with `kernel`.

    Args:
        image: `float [image_sz1 x image_sz2]`.
            Image to filter.
        kernel: `np.uint8 [kernel_sz1 x kernel_sz2]`.
            Top hat `kernel` containing only zeros or ones.

    Returns:
        `float [image_sz1 x image_sz2]`.
            `image` after being top hat filtered with `kernel`.
    """
    if kernel.dtype != np.uint8:
        if sum(np.unique(kernel) == [0, 1]) == len(np.unique(kernel)):
            kernel = kernel.astype(np.uint8)  # kernel must be uint8
        else:
            raise ValueError(f'kernel is of type {kernel.dtype} but must be of data type np.uint8.')
    image_dtype = image.dtype   # so returned image is of same dtype as input
    if image.dtype == int:
        if image.min() >= 0 and image.max() <= np.iinfo(np.uint16).max:
            image = image.astype(np.uint16)
    if not (image.dtype == float or image.dtype == np.uint16):
        raise ValueError(f'image is of type {image.dtype} but must be of data type np.uint16 or float.')
    if np.max(np.mod(kernel.shape, 2) == 0):
        # With even kernel, gives different results to MATLAB
        raise ValueError(f'kernel dimensions are {kernel.shape}. Require all dimensions to be odd.')
    # kernel = ensure_odd_kernel(kernel)  # doesn't work for tophat at start or end.
    return cv2.morphologyEx(image, cv2.MORPH_TOPHAT, kernel).astype(image_dtype)


def dilate(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """
    Dilates `image` with `kernel`, using zero padding.

    Args:
        image: `float [image_sz1 x ... x image_szN]`.
            Image to be dilated.
        kernel: `int [kernel_sz1 x ... x kernel_szN]`.
            Dilation kernel containing only zeros or ones.

    Returns:
        `float [image_sz1 x image_sz2]`.
            `image` after being dilated with `kernel`.
    """
    kernel = ensure_odd_kernel(kernel)
    # mode refers to the padding. We pad with zeros to keep results the same as MATLAB
    return grey_dilation(image, footprint=kernel, mode='constant')
    # return morphology.dilation(image, kernel)


def imfilter(image: np.ndarray, kernel: np.ndarray, padding: Union[float, str] = 0,
             corr_or_conv: str = 'corr', oa: bool = True) -> np.ndarray:
    """
    Copy of MATLAB `imfilter` function with `'output_size'` equal to `'same'`.

    Args:
        image: `float [image_sz1 x image_sz2 x ... x image_szN]`.
            Image to be filtered.
        kernel: `float [kernel_sz1 x kernel_sz2 x ... x kernel_szN]`.
            Multidimensional filter.
        padding: One of the following, indicated which padding to be used.

            - numeric scalar - Input array values outside the bounds of the array are assigned the value `X`.
                When no padding option is specified, the default is `0`.
            - `???symmetric???` - Input array values outside the bounds of the array are computed by
                mirror-reflecting the array across the array border.
            - `???edge???`- Input array values outside the bounds of the array are assumed to equal
                the nearest array border value.
            - `'wrap'` - Input array values outside the bounds of the array are computed by implicitly
                assuming the input array is periodic.
        corr_or_conv:

            - `'corr'` - Performs multidimensional filtering using correlation.
                This is the default when no option specified.
            - `'conv'` - Performs multidimensional filtering using convolution.
        oa: Whether to use oaconvolve or scipy.ndimage.convolve.
            scipy.ndimage.convolve seems to be quicker for smoothing in extract step (3s vs 20s for 50 z-planes).

    Returns:
        `float [image_sz1 x image_sz2 x ... x image_szN]`.
            `image` after being filtered.
    """
    if oa:
        if corr_or_conv == 'corr':
            kernel = np.flip(kernel)
        elif corr_or_conv != 'conv':
            raise ValueError(f"corr_or_conv should be either 'corr' or 'conv' but given value is {corr_or_conv}")
        kernel = ensure_odd_kernel(kernel, 'end')
        if kernel.ndim < image.ndim:
            kernel = np.expand_dims(kernel, axis=tuple(np.arange(kernel.ndim, image.ndim)))
        pad_size = [(int((ax_size-1)/2),)*2 for ax_size in kernel.shape]
        if isinstance(padding, numbers.Number):
            return oaconvolve(np.pad(image, pad_size, 'constant', constant_values=padding), kernel, 'valid')
        else:
            return oaconvolve(np.pad(image, pad_size, padding), kernel, 'valid')
    else:
        if padding == 'symmetric':
            padding = 'reflect'
        elif padding == 'edge':
            padding = 'nearest'
        # Old method, about 3x slower for filtering large 3d image with small 3d kernel
        if isinstance(padding, numbers.Number):
            pad_value = padding
            padding = 'constant'
        else:
            pad_value = 0.0  # doesn't do anything for non-constant padding
        if corr_or_conv == 'corr':
            kernel = ensure_odd_kernel(kernel, 'start')
            return correlate(image, kernel, mode=padding, cval=pad_value)
        elif corr_or_conv == 'conv':
            kernel = ensure_odd_kernel(kernel, 'end')
            return convolve(image, kernel, mode=padding, cval=pad_value)
        else:
            raise ValueError(f"corr_or_conv should be either 'corr' or 'conv' but given value is {corr_or_conv}")


def get_shifts_from_kernel(kernel: jnp.ndarray) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """
    Returns where kernel is positive as shifts in y, x and z.
    I.e. `kernel=jnp.ones((3,3,3))` would return `y_shifts = x_shifts = z_shifts = -1, 0, 1`.
    Args:
        kernel: int [kernel_szY x kernel_szX x kernel_szY]

    Returns:
        - `int [n_shifts]`.
            y_shifts.
        - `int [n_shifts]`.
            x_shifts.
        - `int [n_shifts]`.
            z_shifts.
    """
    shifts = list(jnp.where(kernel > 0))
    for i in range(kernel.ndim):
        shifts[i] = (shifts[i] - (kernel.shape[i] - 1) / 2).astype(int)
    return tuple(shifts)


def manual_convolve_single(image: jnp.ndarray, y_kernel_shifts: jnp.ndarray, x_kernel_shifts: jnp.asarray,
                           z_kernel_shifts: jnp.ndarray, coord: jnp.ndarray) -> float:
    return jnp.sum(image[coord[0] + y_kernel_shifts, coord[1] + x_kernel_shifts, coord[2] + z_kernel_shifts])


@jax.jit
def manual_convolve(image: jnp.ndarray, y_kernel_shifts: jnp.ndarray, x_kernel_shifts: jnp.asarray,
                    z_kernel_shifts: jnp.ndarray, coords: jnp.ndarray) -> jnp.ndarray:
    """
    Finds result of convolution at specific locations indicated by `coords` with binary kernel.
    I.e. instead of convolving whole `image`, just find result at these `points`.

    !!! note
        image needs to be padded before this function is called otherwise get an error when go out of bounds.

    Args:
        image: `int [image_szY x image_szX x image_szZ]`.
            Image to be filtered. Must be 3D.
        y_kernel_shifts: `int [n_nonzero_kernel]`
            Shifts indicating where kernel equals 1.
            I.e. if `kernel = np.ones((3,3))` then `y_shift = x_shift = z_shift = [-1, 0, 1]`.
        x_kernel_shifts: `int [n_nonzero_kernel]`
            Shifts indicating where kernel equals 1.
            I.e. if `kernel = np.ones((3,3))` then `y_shift = x_shift = z_shift = [-1, 0, 1]`.
        z_kernel_shifts: `int [n_nonzero_kernel]`
            Shifts indicating where kernel equals 1.
            I.e. if `kernel = np.ones((3,3))` then `y_shift = x_shift = z_shift = [-1, 0, 1]`.
        coords: `int [n_points x 3]`.
            yxz coordinates where result of filtering is desired.

    Returns:
        `int [n_points]`.
            Result of filtering of `image` at each point in `coords`.
    """
    return jax.vmap(manual_convolve_single, in_axes=(None, None, None, None, 0),
                    out_axes=0)(image, y_kernel_shifts, x_kernel_shifts,z_kernel_shifts, coords)


def imfilter_coords(image: np.ndarray, kernel: np.ndarray, coords: np.ndarray, padding: Union[float, str] = 0,
                    corr_or_conv: str = 'corr') -> Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
    """
    Copy of MATLAB `imfilter` function with `'output_size'` equal to `'same'`.
    Only finds result of filtering at specific locations.

    !!! note
        image and image2 need to be np.int8 and kernel needs to be int otherwise will get cython error.

    Args:
        image: `int [image_szY x image_szX (x image_szZ)]`.
            Image to be filtered. Must be 2D or 3D.
        kernel: `int [kernel_szY x kernel_szX (x kernel_szZ)]`.
            Multidimensional filter, expected to be binary i.e. only contains 0 and/or 1.
        coords: `int [n_points x image.ndims]`.
            Coordinates where result of filtering is desired.
        padding: One of the following, indicated which padding to be used.
            - numeric scalar - Input array values outside the bounds of the array are assigned the value `X`.
                When no padding option is specified, the default is `0`.
            - `???symmetric???` - Input array values outside the bounds of the array are computed by
                mirror-reflecting the array across the array border.
            - `???edge???`- Input array values outside the bounds of the array are assumed to equal
                the nearest array border value.
            - `'wrap'` - Input array values outside the bounds of the array are computed by implicitly
                assuming the input array is periodic.
        corr_or_conv:
            - `'corr'` - Performs multidimensional filtering using correlation.
                This is the default when no option specified.
            - `'conv'` - Performs multidimensional filtering using convolution.

    Returns:
        `int [n_points]`.
            Result of filtering of `image` at each point in `coords`.
    """
    if corr_or_conv == 'corr':
        kernel = np.flip(kernel)
    elif corr_or_conv != 'conv':
        raise ValueError(f"corr_or_conv should be either 'corr' or 'conv' but given value is {corr_or_conv}")
    kernel = ensure_odd_kernel(kernel, 'end')

    # Ensure shape of image and kernel correct
    if image.ndim != coords.shape[1]:
        raise ValueError(f"Image has {image.ndim} dimensions but coords only have {coords.shape[1]} dimensions.")
    if image.ndim == 2:
        image = np.expand_dims(image, 2)
    elif image.ndim != 3:
        raise ValueError(f"image must have 2 or 3 dimensions but given image has {image.ndim}.")
    if kernel.ndim == 2:
        kernel = np.expand_dims(kernel, 2)
    elif kernel.ndim != 3:
        raise ValueError(f"kernel must have 2 or 3 dimensions but given image has {image.ndim}.")
    if kernel.max() > 1:
        raise ValueError(f'kernel is expected to be binary, only containing 0 or 1 but kernel.max = {kernel.max()}')

    if coords.shape[1] == 2:
        # set all z coordinates to 0 if 2D.
        coords = np.append(coords, np.zeros((coords.shape[0], 1), dtype=int), axis=1)
    if (coords.max(axis=0) >= np.array(image.shape)).any():
        raise ValueError(f"Max yxz coordinates provided are {coords.max(axis=0)} but image has shape {image.shape}.")

    pad_size = [(int((ax_size-1)/2),)*2 for ax_size in kernel.shape]
    pad_coords = jnp.asarray(coords) + jnp.array([val[0] for val in pad_size])
    if isinstance(padding, numbers.Number):
        image_pad = jnp.pad(jnp.asarray(image), pad_size, 'constant', constant_values=padding).astype(int)
    else:
        image_pad = jnp.pad(jnp.asarray(image), pad_size, padding).astype(int)
    y_shifts, x_shifts, z_shifts = get_shifts_from_kernel(jnp.asarray(np.flip(kernel)))
    return np.asarray(manual_convolve(image_pad, y_shifts, x_shifts, z_shifts, pad_coords))
