"""
1. Open this in Blender's Text Editor (or run via `blender --python ...`).
2. Adjust the CONFIG block below if needed (it will try sane defaults
   automatically: first movie clip found, tracks from its Stabilization
   panel, anchor frame from that panel, full marker-derived frame range).
3. Run the script (Alt+P in the Text Editor, or the Run Script button).
4. A plane named "StabilizedFootagePlane" (and optionally a check camera)
   will appear, fully keyframed.
5. If the rotation looks mirrored/backwards in your scene, flip
   ROTATION_DIRECTION to -1 and re-run.
"""

import bpy
import math
from mathutils import Vector

# ============================== CONFIG ===================================
CLIP_NAME = ""              # leave blank to auto-use the first movie clip
TRACK_NAMES = []            # leave empty to auto-read from the clip's
                             # Stabilization panel (Translation + Rotation/
                             # Scale track lists); falls back to ALL tracks
                             # if that panel has none assigned.
ANCHOR_FRAME = None         # leave None to use the Stabilization panel's
                             # anchor frame (or the clip's start frame if
                             # 2D Stabilization isn't enabled)
FRAME_START = None          # leave None to use the marker-derived range
FRAME_END = None
ENABLE_ROTATION = None      # None = auto from stab.use_stabilize_rotation;
                             # True/False to force on/o
ENABLE_SCALE = None         # None = auto from stab.use_stabilize_scale;
                             # True/False to force on/off
PLANE_WIDTH = 10.0          # world-space width of the plane, in Blender units
                             # (height is derived automatically to match the
                             # image's aspect ratio)
PLANE_NAME = "StabilizedFootagePlane"
ROTATION_DIRECTION = 1      # flip to -1 if rotation comes out mirrored
CREATE_CHECK_CAMERA = True  # adds a static top-down camera for quick testing
# ===========================================================================


def main():
    scene = bpy.context.scene

    # ---- 1. Resolve the movie clip -----------------------------------
    if CLIP_NAME:
        clip = bpy.data.movieclips[CLIP_NAME]
    else:
        if not bpy.data.movieclips:
            raise RuntimeError("No movie clip found in this file. Load one "
                                "in the Movie Clip Editor first.")
        clip = bpy.data.movieclips[0]

    tracking = clip.tracking
    stab = tracking.stabilization

    # ---- 2. Resolve which tracks to use --------------------------------
    # IMPORTANT: keep the Translation list and the Rotation/Scale list
    # SEPARATE. A previous version of this script merged both into one
    # pool and always fitted a rotation from whatever points were in that
    # pool -- so even with zero tracks assigned to Rotation/Scale in the
    # Stabilization panel, a rotation got computed (and keyframed) anyway,
    # since any 2+ real-world tracked points will show *some* apparent
    # rotation even when the user only wanted translation correction.
    if TRACK_NAMES:
        loc_tracks = [tracking.tracks[name] for name in TRACK_NAMES]
        rot_tracks = list(loc_tracks)  # explicit override: treat all as both
    else:
        loc_tracks, rot_tracks = [], []
        try:
            loc_tracks = list(stab.tracks)
        except AttributeError:
            pass
        try:
            rot_tracks = list(stab.rotation_tracks)
        except AttributeError:
            pass
        if not loc_tracks and not rot_tracks:
            print("WARNING: no Translation or Rotation/Scale tracks found in "
                  "the Stabilization panel. Falling back to using ALL tracks "
                  "in the clip for BOTH translation and rotation. If you only "
                  "want translation, assign tracks to the Stabilization "
                  "panel's Translation list in the Clip Editor and re-run.")
            loc_tracks = list(tracking.tracks)
            rot_tracks = list(tracking.tracks)

    if not loc_tracks:
        raise RuntimeError("No translation/location tracks available to use.")

    def panel_flag(name, default=True):
        try:
            return getattr(stab, name)
