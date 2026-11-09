"""Thread-cap values the slot launcher sets on a governed child.

Extracted verbatim from learn-ukrainian ``tests/common/test_test_thread_caps.py``
at 5aba2adb96e13b3f4a5dc2abbe5ed71b7fb81d00. That file stays product-owned.
"""

EXPECTED = {
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "ORT_NUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
    "RAYON_NUM_THREADS": "1",
}
