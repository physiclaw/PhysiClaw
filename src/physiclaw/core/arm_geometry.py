"""Usable gantry travel, shared by calibration and the mechanical build.

These are total axis spans, not work-coordinate limits: the operator places
the work origin on the phone, independently of the gantry's physical center.
Keep this leaf free of dependencies so CAD checks remain standard-library-only.
"""

X_TRAVEL = 110  # mm — toolhead carriage travel along the X crossbeam
Y_TRAVEL = 200  # mm — gantry travel along the long extrusions
