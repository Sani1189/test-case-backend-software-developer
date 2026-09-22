"""Automated pitch boundary and camera crop engine.

The pipeline detects the playing-field boundary in match video and derives spatial
metrics from it, reporting progress and outcome to the platform over HTTP.

The package is a reusable library: everything needed to run the pipeline is
importable. The process entry point is a thin wrapper in :mod:`trackbox_pitch.cli`.
"""

__version__ = "0.1.0"
