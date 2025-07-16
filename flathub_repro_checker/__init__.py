from importlib import metadata

__version__ = metadata.version(__package__ or "flathub_repro_checker")
del metadata
