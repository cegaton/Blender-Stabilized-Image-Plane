Stabilize-by-plane script for Blender
======================================

What this does
--------------
You've already tracked a few markers on an image sequence in the Movie Clip
Editor (presumably assigned to the Stabilization panel under the Tracking ->
Stabilization tab). This script:

  1. Creates a plane in 3D space (in the XY ground plane, facing +Z).
  2. Textures it with the same image sequence (as an unlit "screen" via an
     Emission shader, so it always shows the raw footage colors).
  3. For every frame, fits the best rotation+translation (no scaling) that
     maps your trackers' anchor-frame positions onto their current-frame
     positions, then keyframes the PLANE with the INVERSE of that transform
     (X/Y location + Z rotation only).

Why the inverse? Because the shake is baked into the image content itself.
To make a tracked feature appear stationary to a fixed top-down camera, the
plane carrying that frame's image has to move opposite to the apparent
on-image drift -- exactly the same principle as 2D stabilization, just
applied to an object transform instead of the pixels.

Note: Blender does not expose the Stabilization panel's internally computed
per-frame numbers via the Python API, so this script independently
recomputes an equivalent correction from the raw marker positions. It will
auto-pick up whichever tracks you've already added to the Translation /
Rotation-Scale lists in that panel (ignoring any scale settings there, since
you only want translate XY + rotate Z).

How to use
----------
1. Open this in Blender's Text Editor (or run via `blender --python ...`).
2. Adjust the CONFIG block below if needed (it will try sane defaults
   automatically: first movie clip found, tracks from its Stabilization    
   panel, anchor frame from that panel, full clip frame range).
3. Run the script (Alt+P in the Text Editor, or the Run Script button).
4. A plane named "StabilizedFootagePlane" (and optionally a check camera)
   will appear, fully keyframed.
5. If the rotation looks mirrored/backwards in your scene, flip
   ROTATION_DIRECTION to -1 and re-run.
