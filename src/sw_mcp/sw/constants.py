"""SolidWorks API enum values as plain ints (no dependency on swconst / a type library).

Values are stable across releases; they come from the public swconst enums.
"""

# swDocumentTypes_e
DOC_NONE, DOC_PART, DOC_ASSEMBLY, DOC_DRAWING = 0, 1, 2, 3
DOC_TYPE_NAMES = {DOC_PART: "part", DOC_ASSEMBLY: "assembly", DOC_DRAWING: "drawing"}
EXT_TO_DOC_TYPE = {".sldprt": DOC_PART, ".sldasm": DOC_ASSEMBLY, ".slddrw": DOC_DRAWING}

# swSelectType_e (the ones get_selection_context understands)
SEL_EDGES = 1
SEL_FACES = 2
SEL_VERTICES = 3
SEL_DATUMPLANES = 4
SEL_DATUMAXES = 5
SEL_DATUMPOINTS = 6
SEL_SKETCHES = 9
SEL_SKETCHSEGS = 10
SEL_SKETCHPOINTS = 11
SEL_DIMENSIONS = 14
SEL_COMPONENTS = 20
SEL_MATES = 21
SEL_BODYFEATURES = 22

# swMateType_e
MATE_TYPE_NAMES = {
    0: "coincident", 1: "concentric", 2: "perpendicular", 3: "parallel", 4: "tangent",
    5: "distance", 6: "angle", 8: "symmetric", 9: "cam_follower", 10: "gear", 11: "width",
    12: "lock_to_sketch", 13: "rack_pinion", 15: "path", 16: "lock", 17: "screw",
    18: "linear_coupler", 19: "universal_joint", 20: "coordinate", 21: "slot", 22: "hinge",
    24: "profile_center", 25: "magnetic",
}

# swDimensionParamType_e (IDimension.GetType)
DIM_LINEAR, DIM_ANGULAR, DIM_INTEGER = 1, 2, 3

# swSketchSegments_e
SKETCH_SEG_NAMES = {0: "line", 1: "arc", 2: "ellipse", 3: "spline", 4: "text", 5: "parabola"}

# swOpenDocOptions_e / swSaveAsOptions_e / swSaveAsVersion_e
OPEN_SILENT = 1
SAVE_SILENT = 1
SAVE_AS_CURRENT_VERSION = 0

# swFileLoadError_e (bit flags)
FILE_LOAD_ERRORS = {
    1: "generic error while opening",
    2: "file not found",
    16: "file ID mismatch",
    1024: "invalid file type",
    8192: "file was saved in a newer SolidWorks version",
    65536: "a different file with the same name is already open",
    131072: "file is protected by rights management",
    262144: "not enough memory or resources",
    524288: "file has no display data",
    1048576: "an add-in interrupted the open",
    2097152: "file needs repair",
    4194304: "file has critical data that needs repair",
    8388608: "SolidWorks is busy",
}

# swFileLoadWarning_e (bit flags) - the ones worth telling the LLM about
FILE_LOAD_WARNINGS = {
    2: "opened read-only",
    4: "file is in use by someone else",
    32: "model needs a rebuild",
    64: "a base part could not be loaded",
    128: "was already open",
    1024: "some referenced files were not found",
}

# swFileSaveError_e (bit flags)
FILE_SAVE_ERRORS = {
    1: "generic save error",
    2: "file is read-only",
    4: "file name is empty",
    8: "file name contains the @ character",
    16: "file is locked by another program or user",
    32: "that save format is not available",
    128: "file already exists and was not overwritten",
    256: "invalid file extension for this document",
    512: "the export needs a selection",
    2048: "path is too long",
    4096: "this save-as format is not supported for this document",
    8192: "referenced documents must be saved first",
}

# swFileSaveWarning_e (bit flags)
FILE_SAVE_WARNINGS = {
    1: "model needs a rebuild",
    2: "some references were not saved",
    8: "file was saved in an older format",
}

# Extensions accepted by save_document(save_as_path=...). Non-native ones are exports.
NATIVE_EXTS = {".sldprt": DOC_PART, ".sldasm": DOC_ASSEMBLY, ".slddrw": DOC_DRAWING}
EXPORT_EXTS = {
    ".step", ".stp", ".igs", ".iges", ".stl", ".3mf", ".x_t", ".x_b", ".sat", ".vrml", ".wrl",
    ".pdf", ".dxf", ".dwg", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".edrw", ".eprt", ".easm",
}


def decode_flags(value: int, table: dict[int, str]) -> list[str]:
    value = int(value or 0)
    out = [text for bit, text in table.items() if value & bit]
    rest = value & ~sum(table)
    if rest:
        out.append(f"code {rest}")
    return out
