import base64
import struct

import numpy as np

# Valhalla configuration constants for speed encoding/decoding.
# BUCKETS_PER_WEEK: Number of 5-minute time buckets in a week.
#   Calculation: 7 days × 24 hours × 60 minutes ÷ 5-minute interval = 2016
BUCKETS_PER_WEEK = 2016

# COEFFICIENT_COUNT: Number of DCT-II (Discrete Cosine Transform Type II) 
#   coefficients used for compressing one week of speed data.
#   Note: This is 200 coefficients, NOT 10! This is critical for encoding/decoding.
COEFFICIENT_COUNT = 200

def compress_speed_buckets(speeds: np.ndarray | list[float], coefficient_count: int = COEFFICIENT_COUNT) -> np.ndarray:
    """
    Compress one week of speed data using DCT-II (Discrete Cosine Transform Type II).
    
    The DCT-II transform is a lossy compression technique that converts time-domain
    speed values into frequency-domain coefficients. This allows efficient storage
    and transmission of weekly traffic patterns while reducing data size significantly.
    
    Parameters
    ----------
    speeds : np.ndarray or list
        A 1D array or list containing exactly 2016 speed values (float or int).
        Each value represents the average speed in km/h for a 5-minute bucket.
        Week layout: starts from Sunday 00:00, ends on Saturday 23:55.
        Example: speeds[0] = Sunday 00:00-00:05, speeds[1] = Sunday 00:05-00:10, etc.
    
    Returns
    -------
    np.ndarray (dtype=int16, shape=(200,))
        Array of 200 integer DCT-II coefficients, dtype int16.
        These coefficients encode the frequency content of the weekly speed pattern.
        Can be used for reconstruction via decompress_speed_buckets() or encoding via
        encode_compressed_speeds().
    
    Raises
    ------
    ValueError
        If len(speeds) != 2016, indicating malformed weekly data.
    
    Notes
    -----
    - DCT-II formula: X_k = sum_{n=0}^{N-1} x_n * cos(π * k * (n + 0.5) / N)
    - Normalization: k=0 divides by sqrt(N); k>0 multiplies by sqrt(2/N).
    - The output is quantized to int16 to reduce storage; small precision loss is expected.
    """
    # Validate input: must contain exactly 2016 values for a complete week.
    if len(speeds) != BUCKETS_PER_WEEK:
        raise ValueError(
            f"Speed array must contain exactly {BUCKETS_PER_WEEK} values "
            f"(one 5-minute interval per slot for 7 days). Got {len(speeds)}."
        )
    
    # Convert input to float32 for DCT computation (improves numerical stability).
    speeds = np.array(speeds, dtype=np.float32)
    
    # Perform DCT-II (Discrete Cosine Transform Type II) computation.
    # Formula: X_k = sum_{n=0}^{N-1} x_n * cos(π * k * (n + 0.5) / N)
    # This transforms time-domain speed values to frequency-domain coefficients.
    N = BUCKETS_PER_WEEK  # type: int
    coefficients = np.zeros(COEFFICIENT_COUNT, dtype=np.float32)  # type: np.ndarray
    
    k = np.arange(COEFFICIENT_COUNT)
    n = np.arange(N)
    cos_matrix = np.cos(np.pi * np.outer(k, n + 0.5) / N)
    coefficients = np.dot(cos_matrix, speeds)
    # cos_matrix = np.cos(np.pi * np.outer(k, n + 0.5) / N).astype(np.float32)
    # coefficients = np.dot(cos_matrix, speeds).astype(np.float32)
    
    # Apply normalization
    sqrt_n = np.sqrt(N)
    coefficients[0] /= sqrt_n
    coefficients[1:] *= np.sqrt(2.0 / N)
    
    # Quantize coefficients to int16 to reduce storage size.
    # Rounding is applied before casting to minimize quantization error.
    coefficients_int16 = np.round(coefficients).astype(np.int16)            # type: np.ndarray
    
    return coefficients_int16

def encode_compressed_speeds(coefficients: np.ndarray, coefficient_count: int = COEFFICIENT_COUNT) -> str:
    """
    Encode DCT coefficients into a Base64-encoded string for storage in CSV files.
    
    This function serializes 200 int16 DCT coefficients into a byte array using
    big-endian byte order (network byte order), then encodes as Base64 for safe
    text representation in CSV or other text formats.
    
    Valhalla format specification:
    - 200 int16 coefficients in big-endian byte order
    - No version byte prefix
    - Total size: 200 × 2 = 400 bytes (always)
    - Base64-encoded output: ~534 characters for 400 bytes
    
    Parameters
    ----------
    coefficients : np.ndarray
        1D array of exactly 200 int16 DCT coefficients.
        Typically output from compress_speed_buckets() or load from external source.
        dtype must be compatible with int16 (e.g., int, int32, int16).
    
    Returns
    -------
    str
        Base64-encoded string representing the 400 bytes.
        Safe for embedding in CSV files and JSON payloads.
        Example: "AbCdEf...xYz+/" (534 characters for 200 coefficients).
    
    Raises
    ------
    ValueError
        If len(coefficients) != 200, indicating incorrect coefficient count.
    
    Notes
    -----
    - Big-endian format: most significant byte first. This matches Valhalla's expectation.
    - No compression is applied beyond DCT. Base64 is purely for text encoding.
    - To retrieve coefficients, use decode_compressed_speeds().
    """
    # Validate coefficient count; must be exactly 200 per Valhalla spec.
    if len(coefficients) != COEFFICIENT_COUNT:
        raise ValueError(
            f"Coefficient array must contain exactly {COEFFICIENT_COUNT} values. "
            f"Got {len(coefficients)}."
        )
    
    # Ensure coefficients are int16 dtype for correct byte serialization.
    coefficients = np.array(coefficients, dtype=np.int16)  # type: np.ndarray
    
    # Create a byte array to hold serialized coefficients.
    # Format: each int16 is encoded as 2 bytes in big-endian order.
    byte_array = bytearray()  # type: bytearray
    
    # Serialize each coefficient as 2 big-endian bytes.
    # Big-endian ('>h') ensures network-standard byte ordering required by Valhalla.
    for coef in coefficients:
        # struct.pack('>h', value) converts signed int16 to 2 big-endian bytes.
        # Example: 256 → b'\x01\x00', -1 → b'\xff\xff'
        byte_array.extend(struct.pack('>h', int(coef)))
    
    # Verify the final byte array is exactly 400 bytes (200 coefficients × 2 bytes each).
    # This is a critical check—incorrect size indicates a packing bug or data corruption.
    assert len(byte_array) == 2*COEFFICIENT_COUNT, (
        f"Byte array length error: {len(byte_array)}. Expected 400 bytes "
        f"(200 coefficients × 2 bytes). This indicates a serialization failure."
    )
    
    # Encode bytes to Base64 ASCII string for safe text representation.
    # Base64 roughly increases size by 33% (400 bytes → ~534 characters).
    encoded = base64.b64encode(bytes(byte_array)).decode('ascii')  # type: str
    
    return encoded