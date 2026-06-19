"""
Stabilize-by-plane script for Blender
======================================

What this does
--------------
You've already tracked a few markers on an image sequence in the Movie Clip
Editor (assigned to the Stabilization panel under Tracking -> Stabilization).
This script:

  1. Creates a plane in 3D space (in the XY ground plane, facing +Z).
  2. Textures it with the same image sequence (as an unlit "screen" via an
     Emission shader, so it always shows the raw footage colors).
  3. For every frame, reconstructs Blender's own 2D-stabilization algorithm
     from the raw marker data (translation, then rotation/scale around the
     translation pivot -- see below), then keyframes the PLANE with the
     INVERSE of that (X/Y location + Z rotation + uniform XY scale).

Matching Blender's actual algorithm
------------------------------------
This reconstruction is checked directly against Blender's own source
(source/blender/blenkernel/intern/tracking_stabilize.cc), not just the
manual. The real algorithm:
  1. Translation = the WEIGHTED AVERAGE raw position of the Translation
     tracks (each track's `weight_stab` controls its influence; tracks with
     weight below ~0.005 or a muted marker are excluded for that frame,
     matching `is_effectively_disabled()`). This weighted average position
     IS the pivot point used below.
  2. Each Rotation/Scale track's contribution is measured as a vector from
     that pivot to the track (with the image's aspect ratio applied to the
     x-axis first, matching `rotation_contribution()`). The ANGLE of each
     track is averaged across tracks as a plain WEIGHTED ARITHMETIC MEAN
     (not a vector/circular mean). The SCALE of each track (its distance
     ratio from the pivot, current vs. anchor, with a small additive bias
     to keep points very close to the pivot from blowing up) is averaged as
     a weighted GEOMETRIC MEAN (i.e. the arithmetic mean of its logarithm).
     Both averages use a "quality" factor that further downweights points
     close to the pivot, on top of the track's own weight_stab.
This script implements that math directly, in the same aspect-corrected
unit space Blender uses, INCLUDING the per-track continuity-baseline system
that lets tracks with different start/end frames (e.g. a pan/tilt shot
where the original feature leaves frame and a new one has to take over)
contribute without a jump at the handoff point -- each track's baseline is
calibrated against whatever the already-established tracks were already
reporting at that track's own first usable frame, exactly mirroring
`establish_track_initialization_order` / `init_track_for_stabilization` in
the source.

It also honors the panel's `use_stabilize_rotation` / `use_stabilize_scale`
checkboxes, and the `target_position/rotation/scale` and
`influence_location/rotation/scale` settings.

Why the inverse? Because the shake is baked into the image content itself.
To make a tracked feature appear stationary to a fixed top-down camera, the
plane carrying that frame's image has to move/rotate/scale opposite to the
apparent on-image drift -- the same principle as 2D stabilization, just
applied to an object transform instead of pixels.

Caveats (things this script does NOT attempt to replicate exactly):
  - `use_autoscale` (auto-zoom to cover empty corners introduced by
    stabilization) is a different, separate mechanism in Blender tied to
    corner-coverage of the rendered frame -- it isn't applicable to a 3D
    plane the same way, so it's not implemented here.
  - `scale_max` (a clamp tied to use_autoscale) is likewise not applied.
  - `target_position` / `target_rotation` / `target_scale` (explicit known
    camera-move overrides) ARE applied, but if they're keyframed/animated,
    this script samples whatever their value is once it steps the scene to
    each frame (which is the same per-frame evaluation Blender itself uses).

How to use
----------
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
                             # True/False to force on/off
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
        except AttributeError:
            return default

    if ENABLE_ROTATION is not None:
        rotation_enabled = ENABLE_ROTATION and len(rot_tracks) >= 1
    else:
        rotation_enabled = panel_flag("use_stabilize_rotation") and len(rot_tracks) >= 1

    if ENABLE_SCALE is not None:
        scale_enabled = ENABLE_SCALE and len(rot_tracks) >= 1
    else:
        scale_enabled = panel_flag("use_stabilize_scale") and len(rot_tracks) >= 1

    if rot_tracks and not rotation_enabled:
        print("NOTE: Rotation/Scale tracks exist but 'Stabilize Rotation' is "
              "off in the Stabilization panel (or ENABLE_ROTATION=False) -- "
              "rotation correction disabled, Z rotation stays 0.")
    if rot_tracks and not scale_enabled:
        print("NOTE: Rotation/Scale tracks exist but 'Stabilize Scale' is "
              "off in the Stabilization panel (or ENABLE_SCALE=False) -- "
              "scale correction disabled, plane scale stays 1.0.")

    print("Translation tracks:", [t.name for t in loc_tracks])
    print("Rotation/Scale tracks:", [t.name for t in rot_tracks] if rot_tracks
          else "(none)")
    print(f"Rotation correction: {'enabled' if rotation_enabled else 'disabled'}  "
          f"Scale correction: {'enabled' if scale_enabled else 'disabled'}")

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
    aspect_hw = (height_px / width_px) if width_px else 1.0   # for the plane mesh
    aspect_wh = (width_px / height_px) if height_px else 1.0  # matches Blender's
                                                                # internal "aspect"
                                                                # (x-axis correction)
    plane_width = PLANE_WIDTH
    plane_height = plane_width * aspect_hw

    # Matches Blender's own constants in tracking_stabilize.cc
    SCALE_ERROR_LIMIT_BIAS = 0.01   # damping bias for points close to the pivot
    EPSILON_WEIGHT = 0.005          # below this, a track counts as "disabled"
                                     # for this frame (matches is_effectively_disabled)

    def raw_marker(track, frame):
        """Raw (co, weight) for a track at a frame, or None if unusable --
        matches Blender's get_tracking_data_point()/is_effectively_disabled():
        excludes muted markers and near-zero per-track weight_stab."""
        marker = track.markers.find_frame(frame, exact=False)
        if marker is None or getattr(marker, "mute", False):
            return None
        w = max(track_weight(track), 0.0)
        if w < EPSILON_WEIGHT:
            return None
        return Vector((marker.co[0], marker.co[1])), w

    def track_weight(track):
        """Per-track influence on the stabilizer (Stabilization panel's
        'Stab Weight', evaluated at the scene's CURRENT frame, since it can
        be animated). Falls back to 1.0 (equal weighting) if unavailable."""
        return getattr(track, "weight_stab", 1.0)

    def track_reference_frame(track, anchor_f):
        """The frame with usable data closest to anchor_f for this track --
        its own local starting point for the baseline calibration below.
        Returns None if the track has no usable data anywhere."""
        usable = [m.frame for m in track.markers
                  if raw_marker(track, m.frame) is not None]
        if not usable:
            return None
        return min(usable, key=lambda f: abs(f - anchor_f))

    def build_continuity_baselines(track_list, anchor_f, value_fn, weight_fn):
        """Generic incremental baseline builder, mirroring Blender's gap-
        handling scheme (establish_track_initialization_order +
        init_track_for_stabilization in tracking_stabilize.cc). This is
        what lets tracks with DIFFERENT start/end frames -- e.g. handed off
        partway through a pan once the original feature leaves frame --
        contribute continuously instead of jumping when one track ends and
        another begins.

        For each track, finds its own reference frame (closest usable frame
        to the global anchor), then initializes tracks in order of
        increasing distance from the anchor, batching tracks that share the
        same reference frame. Each track's baseline is set so its
        contribution exactly matches whatever the ALREADY-initialized
        tracks were already reporting at this track's reference frame --
        for the very first batch (sitting at the true anchor frame, where
        no track has been initialized yet), the baseline is simply zero.

        value_fn(track, frame) -> raw value (Vector or float) or None
        weight_fn(track, frame) -> weight (only called when value isn't None)
        Returns (baseline_dict, ref_frame_dict); tracks with no usable data
        anywhere are omitted from both.
        """
        ref_frame = {}
        for t in track_list:
            rf = track_reference_frame(t, anchor_f)
            if rf is not None:
                ref_frame[t] = rf
        order = sorted(ref_frame.items(), key=lambda kv: abs(kv[1] - anchor_f))

        baseline = {}
        initialized = []
        i = 0
        while i < len(order):
            rf = order[i][1]
            batch = []
            j = i
            while j < len(order) and order[j][1] == rf:
                batch.append(order[j][0])
                j += 1

            acc, wsum = None, 0.0
            for t in initialized:
                v = value_fn(t, rf)
                if v is None:
                    continue
                w = weight_fn(t, rf)
                contribution = baseline[t] + v
                acc = contribution * w if acc is None else acc + contribution * w
                wsum += w
            avg_contribution = (acc / wsum) if wsum > EPSILON_WEIGHT else None

            for t in batch:
                v = value_fn(t, rf)
                base_avg = avg_contribution if avg_contribution is not None else v * 0.0
                baseline[t] = base_avg - v
                initialized.append(t)

            i = j
        return baseline, ref_frame

    def aspect_vec(co_raw, pivot_raw):
        """Vector from pivot to a raw marker position, with Blender's x-axis
        aspect correction applied (matches rotation_contribution() in
        tracking_stabilize.cc: `pos[0] *= aspect` where aspect = width/height).
        This is the unit space in which angle, length, the scale bias, and
        the proximity 'quality' factor are all computed."""
        d = co_raw - pivot_raw
        return Vector((d.x * aspect_wh, d.y))

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

    # Match the color space the user has set on the movie clip in the Clip
    # Editor (Clip Properties > Footage Setup > Color Space). A freshly
    # bpy.data.images.load()'d image gets Blender's default guess based on
    # file type/extension, which is frequently wrong for raw camera
    # formats like DPX/EXR (often "Linear"/an ACES space rather than the
    # sRGB default) -- without this, the plane's texture can look right in
    # the Clip Editor but washed out, too dark, or just plain wrong once
    # textured onto the plane and viewed/rendered.
    clip_colorspace = clip.colorspace_settings.name
    try:
        img.colorspace_settings.name = clip_colorspace
        print(f"Texture color space set to match the movie clip: '{clip_colorspace}'")
    except TypeError:
        print(f"WARNING: couldn't set the texture's color space to "
              f"'{clip_colorspace}' (not a valid color space name in this "
              f"file/OCIO config) -- left at Blender's default "
              f"('{img.colorspace_settings.name}'). Check Image Properties "
              f"on the plane's texture and set it manually if needed.")

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

    # ---- 7. Build continuity baselines (handles tracks with different ----
    # ----    start/end frames -- e.g. a pan/tilt shot where the original
    # ----    tracked feature leaves frame and a new one must take over) ---
    def loc_value(t, f):
        r = raw_marker(t, f)
        return r[0] if r else None

    def loc_weight(t, f):
        r = raw_marker(t, f)
        return r[1] if r else 0.0

    loc_baseline, loc_ref_frame = build_continuity_baselines(loc_tracks, anchor_frame, loc_value, loc_weight)

    def report_continuity(label, ref_frame_dict):
        gapped = {t.name: rf for t, rf in ref_frame_dict.items() if rf != anchor_frame}
        if gapped:
            print(f"  {label}: {len(gapped)} track(s) don't cover the anchor "
                  f"frame ({anchor_frame}) and are using gap-continuity "
                  f"correction, calibrated at their own nearest frame: "
                  f"{gapped}")
        else:
            print(f"  {label}: all tracks cover the anchor frame -- no gap "
                  f"correction needed.")

    print("---- Gap-continuity check (handles tracks with different "
          "start/end frames, e.g. handed off mid-pan) ----")
    report_continuity("Translation", loc_ref_frame)

    def loc_pivot_and_translation(frame):
        """Returns (pivot, translation_total) at a frame, or (None, None) if
        no translation track has usable data there. `pivot` is the plain
        weighted-average raw position (used as the rotation/scale pivot,
        matching Blender's r_pivot). `translation_total` is the baseline-
        corrected weighted-average CONTRIBUTION -- the total accumulated
        drift since the anchor frame, continuity-corrected across gaps
        (exactly 0 at the anchor frame by construction)."""
        acc_pos, wsum_pos = Vector((0.0, 0.0)), 0.0
        acc_contrib, wsum_contrib = Vector((0.0, 0.0)), 0.0
        for t in loc_tracks:
            r = raw_marker(t, frame)
            if r is None:
                continue
            co, w = r
            acc_pos += co * w
            wsum_pos += w
            if t in loc_baseline:
                acc_contrib += (loc_baseline[t] + co) * w
                wsum_contrib += w
        if wsum_pos < EPSILON_WEIGHT:
            return None, None
        pivot = acc_pos / wsum_pos
        translation_total = (acc_contrib / wsum_contrib) if wsum_contrib >= EPSILON_WEIGHT else Vector((0.0, 0.0))
        return pivot, translation_total

    _pivot_cache = {}
    def pivot_at(frame):
        if frame not in _pivot_cache:
            p, _ = loc_pivot_and_translation(frame)
            _pivot_cache[frame] = p
        return _pivot_cache[frame]

    def rot_quality_weight(t, f):
        piv = pivot_at(f)
        if piv is None:
            return 0.0
        r = raw_marker(t, f)
        if r is None:
            return 0.0
        co, w = r
        length = aspect_vec(co, piv).length
        quality = 1.0 - math.exp(-(length * length))
        return w * quality

    def rot_angle_value(t, f):
        piv = pivot_at(f)
        if piv is None:
            return None
        r = raw_marker(t, f)
        if r is None:
            return None
        co, _w = r
        v = aspect_vec(co, piv)
        if v.length < 1e-9:
            return None
        return math.atan2(v.y, v.x)

    def rot_logscale_value(t, f):
        piv = pivot_at(f)
        if piv is None:
            return None
        r = raw_marker(t, f)
        if r is None:
            return None
        co, _w = r
        return math.log(aspect_vec(co, piv).length + SCALE_ERROR_LIMIT_BIAS)

    angle_baseline, scale_baseline = {}, {}
    if (rotation_enabled or scale_enabled) and rot_tracks:
        angle_baseline, rot_ref_frame = build_continuity_baselines(
            rot_tracks, anchor_frame, rot_angle_value, rot_quality_weight)
        scale_baseline, _ = build_continuity_baselines(
            rot_tracks, anchor_frame, rot_logscale_value, rot_quality_weight)
        report_continuity("Rotation/Scale", rot_ref_frame)
    print("-------------------------------------------------------------")

    def rot_estimate(frame):
        """Ensemble (theta, scale_det, any_data) at a frame: theta is the
        weighted ARITHMETIC mean of baseline-corrected per-track angles
        (radians, 0 at the anchor frame by construction); scale_det is the
        weighted GEOMETRIC mean (exp of the log-average) of baseline-
        corrected per-track distance ratios (1.0 at the anchor frame)."""
        angle_acc, angle_wsum = 0.0, 0.0
        scale_acc, scale_wsum = 0.0, 0.0
        any_data = False
        for t in rot_tracks:
            w = rot_quality_weight(t, frame)
            if w <= 0.0:
                continue
            if t in angle_baseline:
                av = rot_angle_value(t, frame)
                if av is not None:
                    any_data = True
                    angle_acc += (angle_baseline[t] + av) * w
                    angle_wsum += w
            if t in scale_baseline:
                sv = rot_logscale_value(t, frame)
                if sv is not None:
                    scale_acc += (scale_baseline[t] + sv) * w
                    scale_wsum += w
        theta = (angle_acc / angle_wsum) if angle_wsum > 1e-9 else 0.0
        scale_det = math.exp(scale_acc / scale_wsum) if scale_wsum > 1e-9 else 1.0
        return theta, scale_det, any_data

    # ---- 8. Per-frame fit + keyframe ------------------------------------
    # Set new keyframes to LINEAR interpolation up front (since we key every
    # single frame, there's no need for bezier smoothing/overshoot). Doing
    # it this way -- via the preference that controls newly-inserted
    # keyframes -- avoids touching Action.fcurves directly, whose structure
    # changed in recent Blender versions (layered actions / channelbags).
    prefs_edit = bpy.context.preferences.edit
    original_interp = prefs_edit.keyframe_new_interpolation_type
    prefs_edit.keyframe_new_interpolation_type = 'LINEAR'

    original_scene_frame = scene.frame_current

    observed_locations = []  # for a post-run "did anything actually move" check
    missing_rot_frames = 0

    for frame in range(int(frame_start), int(frame_end) + 1):
        scene_frame = frame + scene_frame_offset
        # Step the actual scene frame so any animated stabilization
        # properties (weight_stab, target_position/rotation/scale,
        # influence_*) evaluate to their correct per-frame value, exactly
        # as Blender's own stabilizer would read them.
        scene.frame_set(scene_frame)

        pivot_f, translation_total_f = loc_pivot_and_translation(frame)
        if pivot_f is None:
            continue  # skip frames where no translation tracker has usable data
        pivot_f_plane = Vector(((pivot_f.x - 0.5) * plane_width, (pivot_f.y - 0.5) * plane_height))
        # translation_total_f is a DELTA (drift since anchor), not an
        # absolute position -- so no -0.5 centering offset here, just scale.
        translation_plane = Vector((translation_total_f.x * plane_width,
                                     translation_total_f.y * plane_height))

        theta, scale_det = 0.0, 1.0
        if (rotation_enabled or scale_enabled) and rot_tracks:
            theta, scale_det, any_data = rot_estimate(frame)
            if not any_data:
                missing_rot_frames += 1
            if not rotation_enabled:
                theta = 0.0
            if not scale_enabled:
                scale_det = 1.0

        # ---- Explicit "known shot move" overrides (target_*) -----------
        # These default to 0/0/1 and are rarely touched, but apply them if
        # the user has set them (they can also be animated -- already
        # picked up correctly since we stepped the scene frame above).
        tgt_pos = Vector((0.0, 0.0))
        tgt_rot = 0.0
        tgt_scale = 1.0
        try:
            tgt_pos = Vector((stab.target_position[0] * plane_width,
                               stab.target_position[1] * plane_height))
            tgt_rot = stab.target_rotation
            tgt_scale = stab.target_scale if stab.target_scale > 0 else 1.0
        except AttributeError:
            pass

        # ---- Influence sliders (blend correction amount toward 0) ------
        infl_loc = getattr(stab, "influence_location", 1.0)
        infl_rot = getattr(stab, "influence_rotation", 1.0)
        infl_scale = getattr(stab, "influence_scale", 1.0)

        theta_eff = (theta + tgt_rot) * infl_rot
        scale_eff = 1.0 + ((scale_det * tgt_scale) - 1.0) * infl_scale
        translation_eff = translation_plane * infl_loc

        r_f = -theta_eff
        s_f = (1.0 / scale_eff) if scale_eff > 1e-9 else 1.0

        cos_r, sin_r = math.cos(r_f), math.sin(r_f)
        # Rotate + scale pivot_f_plane by (s_f, r_f), recenter it back onto
        # itself, then subtract the (baseline-corrected, gap-continuous)
        # translation drift -- this generalizes the simple no-gap formula
        # "rotate the pivot, recenter, done" to also account for tracks
        # that started/ended partway through the sequence.
        rp = Vector((s_f * (cos_r * pivot_f_plane.x - sin_r * pivot_f_plane.y),
                     s_f * (sin_r * pivot_f_plane.x + cos_r * pivot_f_plane.y)))
        t_vec = pivot_f_plane - rp - translation_eff - tgt_pos

        plane_obj.location.x = t_vec.x
        plane_obj.location.y = t_vec.y
        plane_obj.location.z = 0.0
        plane_obj.rotation_euler.z = r_f
        plane_obj.scale.x = s_f
        plane_obj.scale.y = s_f

        observed_locations.append((t_vec.x, t_vec.y, r_f, s_f))

        plane_obj.keyframe_insert(data_path="location", index=0, frame=scene_frame)
        plane_obj.keyframe_insert(data_path="location", index=1, frame=scene_frame)
        plane_obj.keyframe_insert(data_path="rotation_euler", index=2, frame=scene_frame)
        plane_obj.keyframe_insert(data_path="scale", index=0, frame=scene_frame)
        plane_obj.keyframe_insert(data_path="scale", index=1, frame=scene_frame)

    scene.frame_set(original_scene_frame)

    # ---- 8b. Sanity check: did the plane actually move at all? ---------
    if observed_locations:
        xs = [v[0] for v in observed_locations]
        ys = [v[1] for v in observed_locations]
        rs = [v[2] for v in observed_locations]
        ss = [v[3] for v in observed_locations]
        spread = max(max(xs) - min(xs), max(ys) - min(ys),
                     max(rs) - min(rs), max(ss) - min(ss))
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
          f"Rotation: {'enabled' if rotation_enabled else 'disabled (stays 0)'}.  "
          f"Scale: {'enabled' if scale_enabled else 'disabled (stays 1.0)'}.")
    if (rotation_enabled or scale_enabled) and missing_rot_frames:
        print(f"NOTE: {missing_rot_frames} frame(s) had translation data but "
              f"no rotation/scale-track data; rotation/scale were left at "
              f"their neutral values (0 / 1.0) for those specific frames only.")


main()
