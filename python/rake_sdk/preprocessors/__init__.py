"""Document preprocessors — convert and index files before mounting in rake."""
from .pipeline import preprocess_file, preprocess_files
from .postprocessors import postprocess_file, postprocess_files
__all__ = ["preprocess_file", "preprocess_files", "postprocess_file", "postprocess_files"]
