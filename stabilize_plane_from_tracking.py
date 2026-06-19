"""
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
FRAME_START = None          # leave None to use the clip's frame range
FRAME_END = None
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

    rotation_enabled = len(rot_tracks) >= 2
    if rot_tracks and not rotation_enabled:
        print(f"NOTE: only {len(rot_tracks)} rotation track(s) found -- need "
              f"at least 2 to determine a rotation angle, so rotation "
              f"correction is disabled.")

    print("Translation tracks:", [t.name for t in loc_tracks])
    print("Rotation tracks:", [t.name for t in rot_tracks] if rotation_enabled
          else "(none -- rotation correction disabled, Z rotation stays 0)")

    all_used_tracks = list(loc_tracks)
    for t in rot_tracks:
        if t not in all_used_tracks:
            all_used_tracks.append(t)

    # ---- 2b. Diagnostics: catch the #1 cause of "no movement" ----------
    # If a track was only placed (never actually tracked forward/backward
    # through the sequence), it will have just ONE keyframed marker, and
    # every frame query below will silently return that same single point
    # -> zero detected motion -> a "stabilized" plane that never moves.
    print("---- Track diagnostics ----")
    for t in all_used_tracks:
        frames = [m.frame for m in t.markers]
        if frames:
            print(f"  '{t.name}': {len(frames)} markers, "
                  f"frame range {min(frames)}-{max(frames)}")
        else:
            print(f"  '{t.name}': 0 markers (!)")
        if len(frames) <= 1:
            print(f"    WARNING: '{t.name}' has only {len(frames)} marker(s) "
                  f"keyed. It was likely never tracked across the sequence "
                  f"(use Track Forward/Backward in the Clip Editor first).")
    print("----------------------------")

    # ---- 3. Resolve frame range directly from the tracks' own markers --
    # IMPORTANT: clip.frame_start / clip.frame_duration describe where the
    # clip sits in the broader timeline -- they are NOT guaranteed to match
    # the numbering the tracking markers (and the Stabilization anchor
    # frame) actually use. Mixing the two is the #1 cause of a "stabilized"
    # plane that never moves: every find_frame() call ends up querying way
    # outside the real marker range and silently clamps to the same single
    # marker every time. So we derive the frame range straight from the
    # markers themselves, guaranteeing consistency with find_frame().
    all_marker_frames = sorted({m.frame for t in all_used_tracks for m in t.markers})
    if not all_marker_frames:
        raise RuntimeError("None of the selected tracks have any markers.")
    derived_start, derived_end = all_marker_frames[0], all_marker_frames[-1]

    print(f"Detected marker frame range: {derived_start}-{derived_end} "
          f"(from the tracks themselves)")
    print(f"For reference, clip.frame_start={clip.frame_start}, "
          f"clip.frame_duration={clip.frame_duration} "
          f"(this is a different number space if it doesn't overlap above)")

    frame_start = FRAME_START if FRAME_START is not None else derived_start
    frame_end = FRAME_END if FRAME_END is not None else derived_end

    # Markers are numbered in clip-relative terms (1, 2, 3... = 1st, 2nd,
    # 3rd frame of the footage itself), but the keyframes we insert need to
    # land on the SCENE's timeline, where this clip is placed starting at
    # clip.frame_start. Convert between the two with a simple constant
    # offset (assumes clip.frame_offset == 0, the normal case).
    scene_frame_offset = clip.frame_start - derived_start + clip.frame_offset
    print(f"Mapping clip-relative frames {frame_start}-{frame_end} to scene "
          f"frames {frame_start + scene_frame_offset}-{frame_end + scene_frame_offset}")

    anchor_frame = ANCHOR_FRAME
    if anchor_frame is None:
        try:
            anchor_frame = stab.anchor_frame if stab.use_2d_stabilization else frame_start
        except AttributeError:
            anchor_frame = frame_start

    # ---- 4. Geometry helpers --------------------------------------------
    width_px, height_px = clip.size
    aspect = (height_px / width_px) if width_px else 1.0
    plane_width = PLANE_WIDTH
    plane_height = plane_width * aspect

    def marker_local(track, frame):
        """Returns the tracker's position in plane-local 2D coordinates,
        centered at the plane's origin, for a given frame (or None if no
        marker data exists there)."""
        marker = track.markers.find_frame(frame, exact=False)
        if marker is None:
            return None
        co = marker.co  # normalized 0..1, (0,0) = bottom-left of the frame
        return Vector(((co[0] - 0.5) * plane_width, (co[1] - 0.5) * plane_height))

    # ---- 5. Build the plane mesh (with UVs matching the mapping above) -
    mesh = bpy.data.meshes.new(PLANE_NAME)
    hw, hh = plane_width / 2, plane_height / 2
    verts = [(-hw, -hh, 0), (hw, -hh, 0), (hw, hh, 0), (-hw, hh, 0)]
    faces = [(0, 1, 2, 3)]
    mesh.from_pydata(verts, [], faces)
    mesh.uv_layers.new(name="UVMap")
    uv_layer = mesh.uv_layers[0].data
    uv_coords = [(0, 0), (1, 0), (1, 1), (0, 1)]
    for loop_index, uv_co in zip(mesh.polygons[0].loop_indices, uv_coords):
        uv_layer[loop_index].uv = uv_co
    mesh.update()

    plane_obj = bpy.data.objects.new(PLANE_NAME, mesh)
    scene.collection.objects.link(plane_obj)

    # ---- 6. Material with the image sequence ---------------------------
    img = bpy.data.images.load(clip.filepath)
    img.source = 'SEQUENCE'

    mat = bpy.data.materials.new(PLANE_NAME + "_Mat")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    out_node = nt.nodes.new("ShaderNodeOutputMaterial")
    emit_node = nt.nodes.new("ShaderNodeEmission")
    tex_node = nt.nodes.new("ShaderNodeTexImage")

    tex_node.image = img
    tex_node.image_user.use_auto_refresh = True
    tex_node.image_user.use_cyclic = False
    tex_node.image_user.frame_duration = derived_end - derived_start + 1
    tex_node.image_user.frame_start = clip.frame_start

    # Blender's sequence indexing always treats the LOCAL index as starting
    # at #1 (see ImageUser.frame_start docs: "assuming first picture has a
    # #1"), then adds frame_offset to get the real on-disk file number --
    # it does NOT automatically anchor to whatever specific file you loaded.
    # Since our DPX files are numbered in the millions (e.g. ..._1945990.dpx)
    # rather than starting at 1, frame_offset must carry that entire base
    # number, or Blender will go looking for a file named "...0000001.dpx"
    # (which doesn't exist) on every single frame -- exactly the uniform
    # pink texture you saw. Extract the real number from the loaded
    # filename and use THAT as the offset (local index 1 + offset = that
    # number, so offset = number - 1).
    import re
    fname = bpy.path.basename(clip.filepath)
    m = re.search(r'(\d+)(?!.*\d)', fname)  # last run of digits in the filename
    if not m:
        raise RuntimeError(f"Couldn't find a frame number in filename {fname!r} "
                            f"to compute the image sequence offset.")
    first_file_number = int(m.group(1))
    tex_node.image_user.frame_offset = first_file_number - 1
    print(f"Detected sequence file number {first_file_number} from "
          f"'{fname}' -> frame_offset set to {first_file_number - 1}")

    nt.links.new(tex_node.outputs["Color"], emit_node.inputs["Color"])
    nt.links.new(emit_node.outputs[0], out_node.inputs["Surface"])

    tex_node.location = (-300, 0)
    emit_node.location = (-50, 0)
    out_node.location = (200, 0)

    plane_obj.data.materials.append(mat)

    # ---- 6b. Diagnostic: confirm the source media is reachable ---------
    import os
    abspath = bpy.path.abspath(clip.filepath)
    print(f"Texture source file exists on disk right now? "
          f"{os.path.exists(abspath)}  ({abspath})")
    print("If that's False, the volume/mount isn't currently reachable from "
          "this Blender session (the Clip Editor may still preview fine if "
          "it's using a cached proxy instead of the raw file). If it's True "
          "but the texture is still pink in the viewport, try toggling "
          "Material Preview/Rendered shading.")

    # ---- 7. Compute the anchor-frame reference configuration -----------
    def anchor_set(track_list, label):
        pts = []
        for t in track_list:
            p = marker_local(t, anchor_frame)
            if p is None:
                raise RuntimeError(f"Track '{t.name}' ({label}) has no marker "
                                    f"data at anchor frame {anchor_frame}.")
            pts.append(p)
        centroid = sum(pts, Vector((0.0, 0.0))) / len(pts)
        return centroid, [p - centroid for p in pts]

    ca, centered0 = anchor_set(loc_tracks, "translation")
    if rotation_enabled:
        ca_rot, centered0_rot = anchor_set(rot_tracks, "rotation")

    # ---- 8. Per-frame fit + keyframe ------------------------------------
    # Set new keyframes to LINEAR interpolation up front (since we key every
    # single frame, there's no need for bezier smoothing/overshoot). Doing
    # it this way -- via the preference that controls newly-inserted
    # keyframes -- avoids touching Action.fcurves directly, whose structure
    # changed in recent Blender versions (layered actions / channelbags).
    prefs_edit = bpy.context.preferences.edit
    original_interp = prefs_edit.keyframe_new_interpolation_type
    prefs_edit.keyframe_new_interpolation_type = 'LINEAR'

    observed_locations = []  # for a post-run "did anything actually move" check

    missing_rot_frames = 0

    for frame in range(int(frame_start), int(frame_end) + 1):
        Pf = []
        ok = True
        for t in loc_tracks:
            p = marker_local(t, frame)
            if p is None:
                ok = False
                break
            Pf.append(p)
        if not ok:
            continue  # skip frames where a translation tracker has no data

        cc = sum(Pf, Vector((0.0, 0.0))) / len(Pf)

        theta = 0.0
        if rotation_enabled:
            Pf_rot = []
            rot_ok = True
            for t in rot_tracks:
                p = marker_local(t, frame)
                if p is None:
                    rot_ok = False
                    break
                Pf_rot.append(p)
            if rot_ok:
                cc_rot = sum(Pf_rot, Vector((0.0, 0.0))) / len(Pf_rot)
                centeredf_rot = [p - cc_rot for p in Pf_rot]
                num = sum(a.x * c.y - a.y * c.x for a, c in zip(centered0_rot, centeredf_rot))
                den = sum(a.x * c.x + a.y * c.y for a, c in zip(centered0_rot, centeredf_rot))
                theta = math.atan2(num, den) * ROTATION_DIRECTION
            else:
                missing_rot_frames += 1
                # keep theta = 0.0 for this frame; translation still applies

        cos_t, sin_t = math.cos(theta), math.sin(theta)
        # Rotate cc by -theta
        rcc = Vector((cos_t * cc.x + sin_t * cc.y, -sin_t * cc.x + cos_t * cc.y))
        t_vec = ca - rcc
        phi = -theta

        plane_obj.location.x = t_vec.x
        plane_obj.location.y = t_vec.y
        plane_obj.location.z = 0.0
        plane_obj.rotation_euler.z = phi

        observed_locations.append((t_vec.x, t_vec.y, phi))

        scene_frame = frame + scene_frame_offset
        plane_obj.keyframe_insert(data_path="location", index=0, frame=scene_frame)
        plane_obj.keyframe_insert(data_path="location", index=1, frame=scene_frame)
        plane_obj.keyframe_insert(data_path="rotation_euler", index=2, frame=scene_frame)

    # ---- 8b. Sanity check: did the plane actually move at all? ---------
    if observed_locations:
        xs = [v[0] for v in observed_locations]
        ys = [v[1] for v in observed_locations]
        rs = [v[2] for v in observed_locations]
        spread = max(max(xs) - min(xs), max(ys) - min(ys), max(rs) - min(rs))
        if spread < 1e-6:
            print("WARNING: the computed plane transform is IDENTICAL on "
                  "every frame (no motion detected at all). This almost "
                  "always means the tracks being used were never actually "
                  "tracked across the sequence -- check the diagnostics "
                  "printed above for tracks with only 1 marker, then use "
                  "Track Forward/Backward (or Track Sequence) on them in "
                  "the Clip Editor and re-run this script.")

    # ---- 9. Restore the user's original interpolation preference --------
    prefs_edit.keyframe_new_interpolation_type = original_interp

    # ---- 10. Optional: a static top-down check camera -------------------
    if CREATE_CHECK_CAMERA:
        cam_data = bpy.data.cameras.new("StabilizationCheckCam")
        cam_obj = bpy.data.objects.new("StabilizationCheckCam", cam_data)
        cam_obj.location = (0, 0, plane_width * 2)
        cam_obj.rotation_euler = (0, 0, 0)  # points straight down -Z by default
        scene.collection.objects.link(cam_obj)
        cam_data.type = 'ORTHO'
        cam_data.ortho_scale = plane_width * 1.2

    print(f"Done. Created '{plane_obj.name}', keyframed scene frames "
          f"{frame_start + scene_frame_offset}-{frame_end + scene_frame_offset} "
          f"(anchor frame {anchor_frame}, clip-relative). "
          f"Rotation correction: {'enabled' if rotation_enabled else 'disabled (Z rotation stays 0)'}.")
    if rotation_enabled and missing_rot_frames:
        print(f"NOTE: {missing_rot_frames} frame(s) had translation data but "
              f"no rotation-track data; rotation was left at 0 for those "
              f"specific frames only.")


main()
