"""
Wallpaper image processing back-end for Superpaper.

Applies image corrections, crops, merges etc. and sets the wallpaper
with native platform methods whenever possible.

Written by Henri Hänninen, copyright 2019 under MIT licence.
"""

import configparser
import math
import os
import platform
import subprocess
import sys
from operator import itemgetter
from threading import Lock, Thread, Timer

from PIL import Image
from screeninfo import get_monitors

import superpaper.sp_logging as sp_logging
from superpaper.message_dialog import show_message_dialog
from superpaper.sp_paths import CONFIG_PATH, TEMP_PATH

if platform.system() == "Windows":
    import ctypes
elif platform.system() == "Linux":
    # KDE has special needs
    if os.environ.get("DESKTOP_SESSION") in ["/usr/share/xsessions/plasma", "plasma"]:
        import dbus


# Global constants

NUM_DISPLAYS = 0
# list of display resolutions (width,height), use tuples.
RESOLUTION_ARRAY = []
# list of display offsets (width,height), use tuples.
DISPLAY_OFFSET_ARRAY = []

G_WALLPAPER_CHANGE_LOCK = Lock()
G_SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".webp")
G_SET_COMMAND_STRING = ""



class RepeatedTimer(object):
    """Threaded timer used for slideshow."""
    # Credit:
    # https://stackoverflow.com/questions/3393612/run-certain-code-every-n-seconds/13151299#13151299
    def __init__(self, interval, function, *args, **kwargs):
        self._timer = None
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.is_running = False
        self.start()

    def _run(self):
        self.is_running = False
        self.start()
        self.function(*self.args, **self.kwargs)

    def start(self):
        """Starts timer."""
        if not self.is_running:
            self._timer = Timer(self.interval, self._run)
            self._timer.daemon = True
            self._timer.start()
            self.is_running = True

    def stop(self):
        """Stops timer."""
        self._timer.cancel()
        self.is_running = False


class Display():
    """
    Stores refined data of a display.

    Computes PPI if data is available. Stores non-negative translated offsets.
    """
    def __init__(self, monitor):
        self.resolution = (monitor.width, monitor.height)
        self.digital_offset = (monitor.x, monitor.y)
        self.phys_size_mm = tuple(
            sorted(
                [monitor.width_mm, monitor.height_mm],
                reverse=bool(self.resolution[0]>self.resolution[1])
            )
        )   # Take care that physical rotation matches resolution.
        self.detected_phys_size_mm = self.phys_size_mm
        self.ppi = None
        self.ppi_norm_resolution = None
        self.ppi_norm_offset = None
        self.ppi_norm_bezels = (0, 0)
        self.perspective_angles = (0, 0)
        self.name = monitor.name
        if self.resolution and self.phys_size_mm:
            self.ppi = self.compute_ppi()

    def __str__(self):
        return (
            f"Display("
            f"resolution={self.resolution}, "
            f"digital_offset={self.digital_offset}, "
            f"phys_size_mm={self.phys_size_mm}, "
            f"detected_phys_size_mm={self.detected_phys_size_mm}, "
            f"ppi={self.ppi}, "
            f"ppi_norm_resolution={self.ppi_norm_resolution}, "
            f"ppi_norm_offset={self.ppi_norm_offset}, "
            f"ppi_norm_bezels={self.ppi_norm_bezels}, "
            f"perspective_angles={self.perspective_angles}, "
            f"name={self.name!r}"
            f")"
        )

    def __eq__(self, other):
        return bool(
            self.resolution == other.resolution and
            self.digital_offset == other.digital_offset and
            self.detected_phys_size_mm == other.detected_phys_size_mm
        )

    def __hash__(self):
        return hash((self.resolution, self.digital_offset, self.detected_phys_size_mm))

    def diagonal_size(self):
        diag_mm = math.sqrt( self.phys_size_mm[0]**2 + self.phys_size_mm[1]**2 )
        diag_in = round(diag_mm / 25.4, 1)
        return (round(diag_mm), diag_in)

    def compute_ppi(self):
        if self.phys_size_mm[0]:
            ppmm_horiz = self.resolution[0]/self.phys_size_mm[0]
        else:
            if sp_logging.DEBUG:
                sp_logging.G_LOGGER.info(
                    "Display.compute_ppi: self.phys_size_mm[0] was 0."
                )
            return None
        if self.phys_size_mm[1]:
            ppmm_vert = self.resolution[1]/self.phys_size_mm[1]
        else:
            if sp_logging.DEBUG:
                sp_logging.G_LOGGER.info(
                    "Display.compute_ppi: self.phys_size_mm[1] was 0."
                )
            return None
        if abs(ppmm_horiz/ppmm_vert - 1) > 0.01:
            if sp_logging.DEBUG:
                sp_logging.G_LOGGER.info(
                    "WARNING: Horizontal and vertical PPI do not match! hor: %s, ver: %s",
                    ppmm_horiz * 25.4, ppmm_vert * 25.4
                )
                sp_logging.G_LOGGER.info(str(self))
        return ppmm_horiz * 25.4  # inch has 25.4 times the pixels of a millimeter.

    def translate_offset(self, translate_tuple):
        """Move offset point by subtracting the input point.

        This takes the top left most corner of the canvas to (0,0)
        and retains relative offsets between displays as they should be.
        """
        old_offsets = self.digital_offset
        self.digital_offset = (
            old_offsets[0] - translate_tuple[0],
            old_offsets[1] - translate_tuple[1]
        )

    def ppi_and_physsize_from_diagonal_inch(self, diag_inch):
        """
        If physical size detection fails, it can be computed by
        asking the user to enter the diagonal dimension of the monitor
        in inches.
        """
        height_to_width_ratio = self.resolution[1]/self.resolution[0]
        phys_width_inch = diag_inch / math.sqrt(1 + height_to_width_ratio**2)
        phys_height_inch = height_to_width_ratio * phys_width_inch

        self.phys_size_mm = (phys_width_inch * 25.4, phys_height_inch * 25.4)
        self.ppi = self.resolution[0] / phys_width_inch

        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info(
                "Updated PPI = %s and phys_size_mm = %s based on diagonal size: %s inches",
                self.ppi,
                self.phys_size_mm,
                diag_inch
            )
            sp_logging.G_LOGGER.info(
                str(self)
            )

class DisplayLight():
    """Small class to store resolution and position data a kin to full Display."""
    def __init__(self, res, off, bez):
        self.resolution = res
        self.digital_offset = off
        if bez:
            self.ppi_norm_bezels = bez
        else:
            self.ppi_norm_bezels = (0, 0)

    def __str__(self):
        return (
            f"DisplayLight("
            f"resolution={self.resolution}, "
            f"digital_offset={self.digital_offset}, "
            f"ppi_norm_bezels={self.ppi_norm_bezels} "
            f")"
        )


class DisplaySystem():
    """
    Handle the display system as a whole, applying user data such as
    bezel corrections, offsets, physical layout, and produces
    resolutions and offsets that are used to set the wallpaper
    in advanced mode.
    """

    def __init__(self):
        self.disp_list = get_display_data()
        self.compute_ppinorm_resolutions()

        # Data
        self.use_user_diags = False

        # self.update_bezels([(10, 20), (15, 0)]) # TODO usage in load
        self.load_system()

    def __eq__(self, other):
        return bool(tuple(self.disp_list) == tuple(other.disp_list))

    def __hash__(self):
        return hash(tuple(self.disp_list))

    def max_ppi(self):
        return max([disp.ppi for disp in self.disp_list])

    def get_normalized_ppis(self):
        """Return list of PPI values normalized to the max_ppi."""
        max_ppi = self.max_ppi()
        return [disp.ppi/max_ppi for disp in self.disp_list]

    def compute_ppinorm_resolutions(self):
        """Update disp_list PPI density normalized sizes of the real resolutions."""
        rel_ppis = self.get_normalized_ppis()
        for r_ppi, dsp in zip(rel_ppis, self.disp_list):
            dsp.ppi_norm_resolution = (
                round(dsp.resolution[0] / r_ppi),
                round(dsp.resolution[1] / r_ppi)
            )

    def get_ppi_norm_crops(self):
        """Returns list of ppi_norm crop tuples to cut from ppi_norm canvas.

        A valid crop is a 4-tuple: (left, top, right, bottom).
        """
        crops = []
        for dsp in self.disp_list:
            left_top = dsp.ppi_norm_offset[0]
            right_btm = (
                dsp.ppi_norm_resolution[0] + dsp.ppi_norm_offset[0],
                dsp.ppi_norm_resolution[1] + dsp.ppi_norm_offset[1],
            )
            crops.append(left_top + right_btm)
        return crops

    def fits_in_column(self, disp, col):
        """Test if IN DEKSTOP RES the horiz center of disp is below the last disp in the col."""
        col_last_disp = col[-1]
        disp_cntr = (disp.digital_offset[0] + disp.digital_offset[0] + disp.resolution[0])/2 #(left+right)/2
        col_last_left = col_last_disp.digital_offset[0]
        col_last_right = col_last_disp.digital_offset[0] + col_last_disp.resolution[0]
        if (disp_cntr > col_last_left and disp_cntr < col_last_right):
            return True
        else:
            return False

    def column_size(self, col):
        width = max([dsp.ppi_norm_resolution[0] + dsp.ppi_norm_bezels[0] for dsp in col])
        height = sum([dsp.ppi_norm_resolution[1] + dsp.ppi_norm_bezels[1] for dsp in col])
        return (width, height)


    def compute_initial_preview_offsets(self):
        """
        Uses desktop layout data to arrange the displays in their
        physical dimensions in to horizontally centered columns and
        then concatenating these columns horizontally centered, with
        each columns width being that of the widest display in the
        column. Display list needs to be sorted so that displays in
        a column are together and then the columns progress left
        to right.

        Column composition is TESTED with resolution but column SIZES
        are in PPI normalized resolutions to reflect the physical sizes
        of the displays.
        """
        # Construct columns from disp_list
        columns = []
        work_col = []
        for dsp in self.disp_list:
            if work_col == []:
                work_col.append(dsp)
            else:
                if self.fits_in_column(dsp, work_col):
                    work_col.append(dsp)
                else:
                    columns.append(work_col)
                    work_col = [dsp]
                    if dsp == self.disp_list[-1]:
                        columns.append(work_col)

        # print("columns", columns)

        # Tile columns on to the plane with vertical centering
        col_sizes = [self.column_size(col) for col in columns]
        max_col_h = max([sz[1] for sz in col_sizes])
        col_left_tops = []
        current_left = 0
        for sz in col_sizes:
            col_left_tops.append(
                (
                    current_left,
                    round((max_col_h - sz[1])/2)
                )
            )
            current_left += sz[0]
        # print("col_left_tops", col_left_tops)

        # Tile displays in columns onto the plane with horizontal centering
        # within the column. Anchor columns to col_left_tops.
        for col, col_anchor in zip(columns, col_left_tops):
            current_top = 0
            max_dsp_w = max([dsp.ppi_norm_resolution[0] + dsp.ppi_norm_bezels[0] for dsp in col])
            for dsp in col:
                dsp_w = dsp.ppi_norm_resolution[0] + dsp.ppi_norm_bezels[0]
                dsp.ppi_norm_offset = (
                    col_anchor[0]
                    + round((max_dsp_w - dsp_w)/2),
                    col_anchor[1] + current_top
                )
                # print(dsp.ppi_norm_offset)
                current_top += dsp.ppi_norm_resolution[1] + dsp.ppi_norm_bezels[1]
        # Update offsets to disp_list
        flattened_cols = [dsp for col in columns for dsp in col]
        for scope_dsp, dsp in zip(flattened_cols, self.disp_list):
            dsp.ppi_norm_offset = scope_dsp.ppi_norm_offset
        # print("PPI NORM RESOLUTIONS AND OFFSETS")
        # print([(dsp.ppi_norm_resolution, dsp.ppi_norm_offset) for dsp in self.disp_list])

    def get_disp_list(self, use_ppi_norm = False):
        if use_ppi_norm:
            disp_l = []
            for dsp in self.disp_list:
                disp_l.append(
                    DisplayLight(
                        dsp.ppi_norm_resolution,
                        dsp.ppi_norm_offset,
                        dsp.ppi_norm_bezels
                    )
                )
            return disp_l
        else:
            disp_l = self.disp_list
            return disp_l


    def update_ppinorm_offsets(self, offsets, bezels_included = False):
        """Write ppi_norm resolution offsets as determined
        in the GUI into Displays."""
        self.offsets_include_bezels = bezels_included
        for dsp, offs in zip(self.disp_list, offsets):
            dsp.ppi_norm_offset = offs

    def update_bezels(self, bezels_mm):

        # test that input values are positive
        for bez_pair in bezels_mm:
            for bez in bez_pair:
                if bez < 0:
                    msg = "Bezel thickness must be a non-negative number, {} was entered.".format(bez)
                    sp_logging.G_LOGGER.info(msg)
                    show_message_dialog(msg, "Error")
                    return -1
        # convert to normalized pixel units
        max_ppmm = self.max_ppi() / 25.4
        bezels_ppi_norm = [(bz[0] * max_ppmm, bz[1] * max_ppmm) for bz in bezels_mm]
        for bz_px, dsp in zip(bezels_ppi_norm, self.disp_list):
            dsp.ppi_norm_bezels = bz_px
            sp_logging.G_LOGGER.info("update_bezels: %s", bz_px)
        self.compute_initial_preview_offsets()

    def bezels_in_mm(self):
        """Return list of bezel thicknesses in millimeters."""
        bezels_mm = []
        max_ppmm = self.max_ppi() / 25.4
        for dsp in self.disp_list:
            bezels_mm.append(
                (
                    round(dsp.ppi_norm_bezels[0] / max_ppmm, 2),
                    round(dsp.ppi_norm_bezels[1] / max_ppmm, 2)
                )
            )
        return bezels_mm

    def update_display_diags(self, diag_inches):
        """Overwrite detected display sizes with user input."""
        if diag_inches == "auto":
            self.use_user_diags = False
            for dsp in self.disp_list:
                dsp.phys_size_mm = dsp.detected_phys_size_mm
                dsp.ppi = dsp.compute_ppi()
            self.compute_ppinorm_resolutions()
            self.compute_initial_preview_offsets()
            self.save_system()
        else:
            self.use_user_diags = True
            for dsp, diag in zip(self.disp_list, diag_inches):
                dsp.ppi_and_physsize_from_diagonal_inch(diag)
            self.compute_ppinorm_resolutions()
            self.compute_initial_preview_offsets()
            self.save_system()

    def update_perspective_angles(self, angles):
        """Write perspective angle pairs to their respective Displays.

        After this writing process every Display is assumed to have
        a persepective angle pair (float, float), where floats are
        horizontal and vertical rotations of the display. Display with
        angles (0, 0) defines the reference plane of perspective
        corrections.
        """
        # self.use_perspective = True # TODO is a boolean needed for toggling?
        for dsp, angl in zip(self.disp_list, angles):
            dsp.perspective_angles = angl


    def save_system(self):
        """Save the current DisplaySystem instance user given data
        in a central file (CONFIG_PATH/display_systems.dat).

        Data is saved with a DisplaySystem specific has as the key,
        and data saved include:
            - ppi_norm offsets which contain any given bezel thicknesses
            - bezel (bez+gap+bez) sizes for (right_b, bottom_b)
            - display diagonal sizes if any of them are manually changed
            - rotation angles of displays for perspective correction
        """
        archive_file = os.path.join(CONFIG_PATH, "display_systems.dat")
        instance_key = hash(self)

        # collect data for saving
        ppi_norm_offsets = []
        bezel_mms = self.bezels_in_mm()
        diagonal_inches = []
        perspective_angles = []
        for dsp in self.disp_list:
            ppi_norm_offsets.append(dsp.ppi_norm_offset)
            diagonal_inches.append(dsp.diagonal_size()[1])
            perspective_angles.append(dsp.perspective_angles)
        if not self.use_user_diags:
            diagonal_inches = None

        # load previous configs if file is found
        config = configparser.ConfigParser()
        if os.path.exists(archive_file):
            config.read(archive_file)

        # entering data to config under instance_key
        config[instance_key] = {
            "ppi_norm_offsets": list_to_str(ppi_norm_offsets, item_len=2),
            "bezel_mms": list_to_str(bezel_mms, item_len=2),
            "user_diagonal_inches": list_to_str(diagonal_inches, item_len=1),
            "perspective_angles": list_to_str(perspective_angles, item_len=2)
        }

        sp_logging.G_LOGGER.info(
            "Saving DisplaySystem: key: %s, ppi_norm_offsets: %s, "
            "bezel_mms: %s, user_diagonal_inches: %s, perspective_angles: %s",
            instance_key,
            ppi_norm_offsets,
            bezel_mms,
            diagonal_inches,
            perspective_angles
        )

        # write config to file
        with open(archive_file, 'w') as configfile:
            config.write(configfile)


    def load_system(self):
        """Try to load system data from database based on initialization data,
        i.e. the Display list. If no pre-existing system is found, try to guess
        the system topology and update disp_list"""
        archive_file = os.path.join(CONFIG_PATH, "display_systems.dat")
        instance_key = str(hash(self))
        found_match = False

        # check if file exists and if the current key exists in it
        if os.path.exists(archive_file):
            config = configparser.ConfigParser()
            config.read(archive_file)
            sp_logging.G_LOGGER.info("config.sections: %s", config.sections())
            if instance_key in config:
                found_match = True
            else:
                sp_logging.G_LOGGER.info("load: system not found with hash %s", instance_key)
        else:
            sp_logging.G_LOGGER.info("load_system: archive_file not found: %s", archive_file)

        if found_match:
            # read values
            # and push them into self.disp_list
            instance_data = config[instance_key]
            ppi_norm_offsets = str_to_list(instance_data["ppi_norm_offsets"],
                                           item_len=2)
            bezel_mms = str_to_list(instance_data["bezel_mms"],
                                    item_len=2)
            bezel_mms = [(round(bez[0], 2), round(bez[1], 2)) for bez in bezel_mms]
            diagonal_inches = str_to_list(instance_data["user_diagonal_inches"],
                                          item_len=1)
            perspective_angles = str_to_list(instance_data["perspective_angles"],
                                             item_len=2)
            sp_logging.G_LOGGER.info(
                "DisplaySystem loaded: P.N.Offs: %s, "
                "bezel_mmṣ: %s, "
                "user_diagonal_inches: %s, "
                "perspective_angles: %s",
                ppi_norm_offsets, bezel_mms, diagonal_inches, perspective_angles
            )
            self.update_ppinorm_offsets(ppi_norm_offsets) # Bezels & user diagonals always included.
            self.update_bezels(bezel_mms)
            if diagonal_inches:
                self.update_display_diags(diagonal_inches)
                self.compute_ppinorm_resolutions()
            self.update_perspective_angles(perspective_angles)
        else:
            # Continue without data
            self.compute_initial_preview_offsets()



def list_to_str(lst, item_len=1):
    """Format lists as ,(;) separated strings."""
    if item_len == 1:
        if lst:
            return ",".join(str(lst_itm) for lst_itm in lst)
        else:
            return "None"
    else:
        joined_items = []
        for sub_lst in lst:
            joined_items.append(",".join(str(sub_itm) for sub_itm in sub_lst))
        return ";".join(joined_items)

def str_to_list(joined_list, item_len=1):
    """Extract list from joined_list."""
    if item_len == 1:
        if joined_list in [None, "None"]:
            return None
        split_list = joined_list.split(",")
        conv_list = []
        for item in split_list:
            try:
                val = int(item)
            except ValueError:
                try:
                    val = float(item)
                except ValueError:
                    sp_logging.G_LOGGER.info(
                        "str_to_list: ValueError: not int or float: %s", item
                    )
            conv_list.append(val)
        return conv_list
    else:
        split_list = joined_list.split(";")
        conv_list = []
        for item in split_list:
            split_item = item.split(",")
            conv_item = []
            for sub_item in split_item:
                try:
                    val = int(sub_item)
                except ValueError:
                    try:
                        val = float(sub_item)
                    except ValueError:
                        sp_logging.G_LOGGER.info(
                            "str_to_list: ValueError: not int or float: %s", sub_item
                        )
                conv_item.append(val)
            conv_list.append(tuple(conv_item))
        return conv_list

def extract_global_vars(disp_list):
    res_arr = []
    off_arr = []
    for disp in disp_list:
        res_arr.append(disp.resolution)
        off_arr.append(disp.digital_offset)
    return [res_arr, off_arr]

def get_display_data():
    """
    Updates global display variables: number of displays, resolutions and offsets.

    Returns a list of Display objects, one for each monitor. Offsets are sanitized
    so that they are always non-negative.
    """
    # https://github.com/rr-/screeninfo
    global NUM_DISPLAYS, RESOLUTION_ARRAY, DISPLAY_OFFSET_ARRAY
    RESOLUTION_ARRAY = []
    DISPLAY_OFFSET_ARRAY = []
    monitors = get_monitors()
    NUM_DISPLAYS = len(monitors)

    display_list = []
    for monitor in monitors:
        display_list.append(Display(monitor))
    # Check that there are no negative offsets and fix if any are found.
    leftmost_offset = min([disp.digital_offset[0] for disp in display_list])
    topmost_offset = min([disp.digital_offset[1] for disp in display_list])
    if leftmost_offset < 0 or topmost_offset < 0:
        for disp in display_list:
            disp.translate_offset((leftmost_offset, topmost_offset))
    # sort display list by digital offsets
    display_list.sort(key=lambda x: x.digital_offset)
    # extract global variables for legacy compatibility
    RESOLUTION_ARRAY, DISPLAY_OFFSET_ARRAY = extract_global_vars(display_list)
    
    if sp_logging.DEBUG:
        sp_logging.G_LOGGER.info(
            "get_display_data output: NUM_DISPLAYS = %s, RES_ARR = %s, OFF_ARR = %s",
            NUM_DISPLAYS,
            RESOLUTION_ARRAY,
            DISPLAY_OFFSET_ARRAY
        )
        for disp in display_list:
            sp_logging.G_LOGGER.info(str(disp))
    return display_list

def compute_canvas(res_array, offset_array):
    """Computes the size of the total desktop area from monitor resolutions and offsets."""
    # Take the subtractions of right-most right - left-most left
    # and bottom-most bottom - top-most top (=0).
    leftmost = 0
    topmost = 0
    right_edges = []
    bottom_edges = []
    for res, off in zip(res_array, offset_array):
        right_edges.append(off[0]+res[0])
        bottom_edges.append(off[1]+res[1])
    # Right-most edge.
    rightmost = max(right_edges)
    # Bottom-most edge.
    bottommost = max(bottom_edges)
    canvas_size = [rightmost - leftmost, bottommost - topmost]
    if sp_logging.DEBUG:
        sp_logging.G_LOGGER.info("Canvas size: %s", canvas_size)
    return canvas_size


def compute_ppi_corrected_res_array(res_array, ppi_list_rel_density):
    """Return ppi density normalized sizes of the real resolutions."""
    eff_res_array = []
    for i in range(len(res_array)):
        effw = round(res_array[i][0] / ppi_list_rel_density[i])
        effh = round(res_array[i][1] / ppi_list_rel_density[i])
        eff_res_array.append((effw, effh))
    return eff_res_array


# resize image to fill given rectangle and do a centered crop to size.
# Return output image.
def resize_to_fill(img, res):
    """Resize image to fill given rectangle and do a centered crop to size."""
    image_size = img.size  # returns image (width,height)
    if image_size == res:
        # input image is already of the correct size, no action needed.
        return img
    image_ratio = image_size[0] / image_size[1]
    target_ratio = res[0] / res[1]
    # resize along the shorter edge to get an image that is at least of the
    # target size on the shorter edge.
    if image_ratio < target_ratio:      # img not wide enough / is too tall
        resize_multiplier = res[0] / image_size[0]
        new_size = (
            round(resize_multiplier * image_size[0]),
            round(resize_multiplier * image_size[1]))
        img = img.resize(new_size, resample=Image.LANCZOS)
        # crop vertically to target height
        extra_height = new_size[1] - res[1]
        if extra_height < 0:
            sp_logging.G_LOGGER.info(
                "Error with cropping vertically, resized image \
                wasn't taller than target size.")
            return -1
        if extra_height == 0:
            # image is already at right height, no cropping needed.
            return img
        # (left edge, half of extra height from top,
        # right edge, bottom = top + res[1]) : force correct height
        crop_tuple = (
            0,
            round(extra_height/2),
            new_size[0],
            round(extra_height/2) + res[1])
        cropped_res = img.crop(crop_tuple)
        if cropped_res.size == res:
            return cropped_res
        else:
            sp_logging.G_LOGGER.info(
                "Error: result image not of correct size. crp:%s, res:%s",
                cropped_res.size, res)
            return -1
    elif image_ratio >= target_ratio:      # img not tall enough / is too wide
        resize_multiplier = res[1] / image_size[1]
        new_size = (
            round(resize_multiplier * image_size[0]),
            round(resize_multiplier * image_size[1]))
        img = img.resize(new_size, resample=Image.LANCZOS)
        # crop horizontally to target width
        extra_width = new_size[0] - res[0]
        if extra_width < 0:
            sp_logging.G_LOGGER.info(
                "Error with cropping horizontally, resized image \
                wasn't wider than target size.")
            return -1
        if extra_width == 0:
            # image is already at right width, no cropping needed.
            return img
        # (half of extra from left edge, top edge,
        # right = left + desired width, bottom) : force correct width
        crop_tuple = (
            round(extra_width/2),
            0,
            round(extra_width/2) + res[0],
            new_size[1])
        cropped_res = img.crop(crop_tuple)
        if cropped_res.size == res:
            return cropped_res
        else:
            sp_logging.G_LOGGER.info(
                "Error: result image not of correct size. crp:%s, res:%s",
                cropped_res.size, res)
            return -1


def get_center(res):
    """Computes center point of a resolution rectangle."""
    return (round(res[0] / 2), round(res[1] / 2))


def get_all_centers(resarr_eff, manual_offsets):
    """Computes center points of given resolution list taking into account their offsets."""
    centers = []
    sum_widths = 0
    # get the vertical pixel distance of the center of the left most display
    # from the top.
    center_standard_height = get_center(resarr_eff[0])[1]
    if len(manual_offsets) < len(resarr_eff):
        sp_logging.G_LOGGER.info("get_all_centers: Not enough manual offsets: \
                      %s for displays: %s",
                      len(manual_offsets),
                      len(resarr_eff))
    else:
        for i in range(len(resarr_eff)):
            horiz_radius = get_horizontal_radius(resarr_eff[i])
            # here take the center height to be the same for all the displays
            # unless modified with the manual offset
            center_pos_from_anchor_left_top = (
                sum_widths + manual_offsets[i][0] + horiz_radius,
                center_standard_height + manual_offsets[i][1])
            centers.append(center_pos_from_anchor_left_top)
            sum_widths += resarr_eff[i][0]
    if sp_logging.DEBUG:
        sp_logging.G_LOGGER.info("centers: %s", centers)
    return centers


def get_lefttop_from_center(center, res):
    """Compute top left coordinate of a rectangle from its center."""
    return (center[0] - round(res[0] / 2), center[1] - round(res[1] / 2))


def get_rightbottom_from_lefttop(lefttop, res):
    """Compute right bottom corner of a rectangle from its left top."""
    return (lefttop[0] + res[0], lefttop[1] + res[1])


def get_horizontal_radius(res):
    """Returns half the width of the input rectangle."""
    return round(res[0] / 2)


def compute_crop_tuples(resolution_array_ppinormalized, manual_offsets):
    # Assume the centers of the physical displays are aligned on common
    # horizontal line. If this is not the case one must use the manual
    # offsets defined in the profile for adjustment (and bezel corrections).
    # Anchor positions to the top left corner of the left most display. If
    # its size is scaled up, one will need to adjust the horizontal positions
    # of all the displays. (This is automatically handled by using the
    # effective resolution array).
    # Additionally one must make sure that the highest point of the display
    # arrangement is at y=0.
    crop_tuples = []
    centers = get_all_centers(resolution_array_ppinormalized, manual_offsets)
    for center, res in zip(centers, resolution_array_ppinormalized):
        lefttop = get_lefttop_from_center(center, res)
        rightbottom = get_rightbottom_from_lefttop(lefttop, res)
        crop_tuples.append(lefttop + rightbottom)
    # Translate crops so that the highest point is at y=0 -- remember to add
    # translation to both top and bottom coordinates! Same horizontally.
    # Left-most edge of the crop tuples.
    leftmost = min(crop_tuples, key=itemgetter(0))[0]
    # Top-most edge of the crop tuples.
    topmost = min(crop_tuples, key=itemgetter(1))[1]
    if leftmost == 0 and topmost == 0:
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("crop_tuples: %s", crop_tuples)
        return crop_tuples  # [(left, up, right, bottom),...]
    else:
        crop_tuples_translated = translate_crops(
            crop_tuples, (leftmost, topmost))
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("crop_tuples_translated: %s", crop_tuples_translated)
        return crop_tuples_translated  # [(left, up, right, bottom),...]


def translate_crops(crop_tuples, translate_tuple):
    """Translate crop tuples to be over the image are, i.e. left top at (0,0)."""
    crop_tuples_translated = []
    for crop_tuple in crop_tuples:
        crop_tuples_translated.append(
            (crop_tuple[0] - translate_tuple[0],
             crop_tuple[1] - translate_tuple[1],
             crop_tuple[2] - translate_tuple[0],
             crop_tuple[3] - translate_tuple[1]))
    return crop_tuples_translated


def compute_working_canvas(crop_tuples):
    """Computes effective size of the desktop are taking into account PPI/offsets/bezels."""
    # Take the subtractions of right-most right - left-most left
    # and bottom-most bottom - top-most top (=0).
    leftmost = 0
    topmost = 0
    # Right-most edge of the crop tuples.
    rightmost = max(crop_tuples, key=itemgetter(2))[2]
    # Bottom-most edge of the crop tuples.
    bottommost = max(crop_tuples, key=itemgetter(3))[3]
    canvas_size = [rightmost - leftmost, bottommost - topmost]
    return canvas_size


def span_single_image_simple(profile):
    """
    Spans a single image across all monitors. No corrections.

    This simple method resizes the source image so it fills the whole
    desktop canvas. Since no corrections are applied, no offset dependent
    cuts are needed and so this should work on any monitor arrangement.
    """
    file = profile.next_wallpaper_files()[0]
    if sp_logging.DEBUG:
        sp_logging.G_LOGGER.info(file)
    img = Image.open(file)
    canvas_tuple = tuple(compute_canvas(RESOLUTION_ARRAY, DISPLAY_OFFSET_ARRAY))
    img_resize = resize_to_fill(img, canvas_tuple)
    outputfile = os.path.join(TEMP_PATH, profile.name + "-a.png")
    if os.path.isfile(outputfile):
        outputfile_old = outputfile
        outputfile = os.path.join(TEMP_PATH, profile.name + "-b.png")
    else:
        outputfile_old = os.path.join(TEMP_PATH, profile.name + "-b.png")
    img_resize.save(outputfile, "PNG")
    set_wallpaper(outputfile)
    if os.path.exists(outputfile_old):
        os.remove(outputfile_old)
    return 0


# Take pixel densities of displays into account to have the image match
# physically between displays.
def span_single_image_advanced(profile):
    """
    Applies wallpaper using PPI, bezel, offset corrections.

    Further description todo.
    """
    file = profile.next_wallpaper_files()[0]
    if sp_logging.DEBUG:
        sp_logging.G_LOGGER.info(file)
    img = Image.open(file)
    resolution_array_ppinormalized = compute_ppi_corrected_res_array(
        RESOLUTION_ARRAY, profile.ppi_array_relative_density)

    # Cropping now sections of the image to be shown, USE EFFECTIVE WORKING
    # SIZES. Also EFFECTIVE SIZE Offsets are now required.
    manual_offsets = profile.manual_offsets
    cropped_images = []
    crop_tuples = compute_crop_tuples(resolution_array_ppinormalized, manual_offsets)
    # larger working size needed to fill all the normalized lower density
    # displays. Takes account manual offsets that might require extra space.
    canvas_tuple_eff = tuple(compute_working_canvas(crop_tuples))
    # Image is now the height of the eff tallest display + possible manual
    # offsets and the width of the combined eff widths + possible manual
    # offsets.
    img_workingsize = resize_to_fill(img, canvas_tuple_eff)
    # Simultaneously make crops at working size and then resize down to actual
    # resolution from RESOLUTION_ARRAY as needed.
    for crop, res in zip(crop_tuples, RESOLUTION_ARRAY):
        crop_img = img_workingsize.crop(crop)
        if crop_img.size == res:
            cropped_images.append(crop_img)
        else:
            crop_img = crop_img.resize(res, resample=Image.LANCZOS)
            cropped_images.append(crop_img)
    # Combine crops to a single canvas of the size of the actual desktop
    # actual combined size of the display resolutions
    canvas_tuple_fin = tuple(compute_canvas(RESOLUTION_ARRAY, DISPLAY_OFFSET_ARRAY))
    combined_image = Image.new("RGB", canvas_tuple_fin, color=0)
    combined_image.load()
    for i in range(len(cropped_images)):
        combined_image.paste(cropped_images[i], DISPLAY_OFFSET_ARRAY[i])
    # Saving combined image
    outputfile = os.path.join(TEMP_PATH, profile.name + "-a.png")
    if os.path.isfile(outputfile):
        outputfile_old = outputfile
        outputfile = os.path.join(TEMP_PATH, profile.name + "-b.png")
    else:
        outputfile_old = os.path.join(TEMP_PATH, profile.name + "-b.png")
    combined_image.save(outputfile, "PNG")
    set_wallpaper(outputfile)
    if os.path.exists(outputfile_old):
        os.remove(outputfile_old)
    return 0


def set_multi_image_wallpaper(profile):
    """Sets a distinct image on each monitor.

    Since most platforms only support setting a single image
    as the wallpaper this has to be accomplished by creating a
    composite image based on the monitor offsets and then setting
    the resulting image as the wallpaper.
    """
    files = profile.next_wallpaper_files()
    if sp_logging.DEBUG:
        sp_logging.G_LOGGER.info(str(files))
    img_resized = []
    for file, res in zip(files, RESOLUTION_ARRAY):
        image = Image.open(file)
        img_resized.append(resize_to_fill(image, res))
    canvas_tuple = tuple(compute_canvas(RESOLUTION_ARRAY, DISPLAY_OFFSET_ARRAY))
    combined_image = Image.new("RGB", canvas_tuple, color=0)
    combined_image.load()
    for i in range(len(files)):
        combined_image.paste(img_resized[i], DISPLAY_OFFSET_ARRAY[i])
    outputfile = os.path.join(TEMP_PATH, profile.name + "-a.png")
    if os.path.isfile(outputfile):
        outputfile_old = outputfile
        outputfile = os.path.join(TEMP_PATH, profile.name + "-b.png")
    else:
        outputfile_old = os.path.join(TEMP_PATH, profile.name + "-b.png")
    combined_image.save(outputfile, "PNG")
    set_wallpaper(outputfile)
    if os.path.exists(outputfile_old):
        os.remove(outputfile_old)
    return 0


def errcheck(result, func, args):
    """Error getter for Windows."""
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())


def set_wallpaper(outputfile):
    """
    Master method to set the composed image as wallpaper.

    After the final background image is created, this method
    is called to communicate with the host system to set the
    desktop background. For Linux hosts there is a separate method.
    """
    pltform = platform.system()
    if pltform == "Windows":
        spi_setdeskwallpaper = 20
        spif_update_ini_file = 1
        spif_send_change = 2
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        spiw = user32.SystemParametersInfoW
        spiw.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint]
        spiw.restype = ctypes.c_int
        spiw.errcheck = errcheck
        spi_success = spiw(
            spi_setdeskwallpaper,
            0,
            outputfile,
            spif_update_ini_file | spif_send_change)
        if spi_success == 0:
            sp_logging.G_LOGGER.info("SystemParametersInfo wallpaper set failed with \
spi_success: '%s'", spi_success)
    elif pltform == "Linux":
        set_wallpaper_linux(outputfile)
    elif pltform == "Darwin":
        script = """/usr/bin/osascript<<END
                    tell application "Finder"
                    set desktop picture to POSIX file "%s"
                    end tell
                    END"""
        subprocess.Popen(script % outputfile, shell=True)
    else:
        sp_logging.G_LOGGER.info("Unknown platform.system(): %s", pltform)


def set_wallpaper_linux(outputfile):
    """
    Wallpaper setter for Linux hosts.

    Functionality is based on the DESKTOP_SESSION environment variable,
    if it is not set, like often on window managers such as i3, the default
    behavior is to attempt to use feh as the communication layer with the
    desktop.

    On systems where the variable is set, a native way of setting the
    wallpaper can be used. These are DE specific.
    """
    file = "file://" + outputfile
    set_command = G_SET_COMMAND_STRING
    if sp_logging.DEBUG:
        sp_logging.G_LOGGER.info(file)

    desk_env = os.environ.get("DESKTOP_SESSION")
    if sp_logging.DEBUG:
        sp_logging.G_LOGGER.info("DESKTOP_SESSION is: '%s'", desk_env)

    if desk_env:
        if set_command != "":
            if set_command == "feh":
                subprocess.run(["feh", "--bg-scale", "--no-xinerama", outputfile])
            else:
                command_string_list = set_command.split()
                formatted_command = []
                for term in command_string_list:
                    formatted_command.append(term.format(image=outputfile))
                sp_logging.G_LOGGER.info("Formatted custom command is: '%s'", formatted_command)
                subprocess.run(formatted_command)
        elif desk_env in ["gnome", "gnome-wayland",
                          "unity", "ubuntu",
                          "pantheon", "budgie-desktop"]:
            subprocess.run(["gsettings", "set",
                            "org.gnome.desktop.background", "picture-uri",
                            file])
        elif desk_env in ["cinnamon"]:
            subprocess.run(["gsettings", "set",
                            "org.cinnamon.desktop.background", "picture-uri",
                            file])
        elif desk_env in ["mate"]:
            subprocess.run(["gsettings",
                            "set",
                            "org.mate.background",
                            "picture-filename",
                            outputfile])
        elif desk_env in ["xfce", "xubuntu"]:
            xfce_actions(outputfile)
        elif desk_env in ["lubuntu", "Lubuntu"]:
            try:
                subprocess.run("pcmanfm", "-w", outputfile)
            except OSError:
                try:
                    subprocess.run("pcmanfm-qt", "-w", outputfile)
                except OSError:
                    sp_logging.G_LOGGER.info("Exception: failure to find either command \
'pcmanfm' or 'pcmanfm-qt'. Exiting.")
                    sys.exit(1)
        elif desk_env in ["/usr/share/xsessions/plasma", "plasma"]:
            kdeplasma_actions(outputfile)
        elif "i3" in desk_env or desk_env in ["/usr/share/xsessions/bspwm"]:
            subprocess.run(["feh", "--bg-scale", "--no-xinerama", outputfile])
        else:
            if set_command == "":
                message = "Your DE could not be detected to set the wallpaper. \
You need to set the 'set_command' option in your \
settings file superpaper/general_settings. Exiting."
                sp_logging.G_LOGGER.info(message)
                show_message_dialog(message, "Error")
                sys.exit(1)
            else:
                os.system(set_command.format(image=outputfile))
    else:
        sp_logging.G_LOGGER.info("DESKTOP_SESSION variable is empty, \
attempting to use feh to set the wallpaper.")
        subprocess.run(["feh", "--bg-scale", "--no-xinerama", outputfile])

def set_wallpaper_piecewise(image_piece_list):
    """
    Wallpaper setter that takes already cropped images and sets them
    directly to corresponding monitors on systems where wallpapers
    are set on a monitor by monitor basis.

    This is used when the quick wallpaper change conditions are met,
    see quick_profile_job method, to improve performance on these
    systems.

    Currently supported such systems are KDE Plasma and XFCE.
    """
    pltform = platform.system()
    if pltform == "Linux":
        desk_env = os.environ.get("DESKTOP_SESSION")
        if desk_env in ["/usr/share/xsessions/plasma", "plasma"]:
            kdeplasma_actions(None, image_piece_list)
        elif desk_env in ["xfce", "xubuntu"]:
            xfce_actions(None, image_piece_list)
    else:
        pass


def special_image_cropper(outputfile):
    """
    Crops input image into monitor specific pieces based on display offsets.

    This is needed on systems where the wallpapers are set on a per display basis.
    This means that the composed image needs to be re-cut into pieces which
    are saved separately.
    """
    # file needs to be split into monitor pieces since KDE/XFCE are special
    img = Image.open(outputfile)
    outputname = os.path.splitext(outputfile)[0]
    img_names = []
    crop_id = 0
    for res, offset in zip(RESOLUTION_ARRAY, DISPLAY_OFFSET_ARRAY):
        left = offset[0]
        top = offset[1]
        right = left + res[0]
        bottom = top + res[1]
        crop_tuple = (left, top, right, bottom)
        cropped_img = img.crop(crop_tuple)
        fname = outputname + "-crop-" + str(crop_id) + ".png"
        img_names.append(fname)
        cropped_img.save(fname, "PNG")
        crop_id += 1
    return img_names

def remove_old_temp_files(outputfile):
    """
    This method looks for previous temp images and deletes them.

    Currently only used to delete the monitor specific crops that are
    needed for KDE and XFCE.
    """
    opbase = os.path.basename(outputfile)
    opname = os.path.splitext(opbase)[0]
    # print(opname)
    oldfileid = ""
    if "-a" in opname:
        newfileid = "-a"
        oldfileid = "-b"
        # print(oldfileid)
    elif "-b" in opname:
        newfileid = "-b"
        oldfileid = "-a"
        # print(oldfileid)
    else:
        pass
    if oldfileid:
        # Must take care than only temps of current profile are deleted.
        profilename = opname.strip()[:-2]
        match_string = profilename + oldfileid + "-crop"
        match_string = match_string.strip()
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("Removing images matching with: '%s'",
                                     match_string)
        for temp_file in os.listdir(TEMP_PATH):
            if match_string in temp_file:
                # print(temp_file)
                os.remove(os.path.join(TEMP_PATH, temp_file))

def kdeplasma_actions(outputfile, image_piece_list = None):
    """
    Sets the multi monitor wallpaper on KDE.

    Arguments are path to an image and an optional image piece
    list when one can set the wallpaper from existing cropped
    images. IF image pieces are to be used, call this method
    with outputfile == None.

    This is needed since KDE uses its own scripting language to
    set the desktop background which sets a single image on every
    monitor. This means that the composed image must be cut into
    correct pieces that then are set to their respective displays.
    """

    script = """
// make an array of all desktops with a valid screen
var desktopArray = [];
for(var desktopIndex in desktops()) {{
    var desktop = desktops()[desktopIndex];
    if(desktop.screen != -1) {{
        desktopArray.push(desktop);
    }}
}}

// sort the array based on the (horizontal) desktop position
var i = 1;
while(i < desktopArray.length) {{
    var j = i;
    while(j > 0 && screenGeometry(desktopArray[j-1].screen).left > screenGeometry(desktopArray[j].screen).left) {{
        var temp = desktopArray[j];
        desktopArray[j] = desktopArray[j-1];
        desktopArray[j-1] = temp;
        j = j-1;
    }}
    i = i+1;
}}

var imageFileArray = Array({imagelist});

// set the desired wallpaper
var k = 0;
while(k < desktopArray.length) {{
    var desktop = desktopArray[k];
    desktop.wallpaperPlugin = "org.kde.image";
    desktop.currentConfigGroup = Array("Wallpaper", "org.kde.image", "General");
    desktop.writeConfig("Image", imageFileArray[k]);
    k = k+1;
}}
"""
    if outputfile:
        img_names = special_image_cropper(outputfile)
    elif not outputfile and image_piece_list:
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("KDE: Using image piece list!")
        img_names = image_piece_list
    else:
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("Error! KDE actions called without arguments!")

    filess_img_names = []
    for fname in img_names:
        filess_img_names.append("file://" + fname)
    filess_img_names_str = ', '.join('"' + item + '"' for item in filess_img_names)
    # print(script.format(imagelist=filess_img_names_str))

    sessionb = dbus.SessionBus()
    plasma_interface = dbus.Interface(
        sessionb.get_object(
            "org.kde.plasmashell",
            "/PlasmaShell"),
        dbus_interface="org.kde.PlasmaShell")
    plasma_interface.evaluateScript(
        script.format(imagelist=filess_img_names_str)
    )

    # Delete old images after new ones are set
    if outputfile:
        remove_old_temp_files(outputfile)


def xfce_actions(outputfile, image_piece_list = None):
    """
    Sets the multi monitor wallpaper on XFCE.

    This is needed since XFCE uses its own scripting interface to
    set the desktop background which sets a single image on every
    monitor. This means that the composed image must be cut into
    correct pieces that then are set to their respective displays.
    """
    if outputfile:
        img_names = special_image_cropper(outputfile)
    elif not outputfile and image_piece_list:
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("XFCE: Using image piece list!")
        img_names = image_piece_list
    else:
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("Error! XFCE actions called without arguments!")

    monitors = []
    for mon_index in range(NUM_DISPLAYS):
        monitors.append("monitor" + str(mon_index))

    read_prop = subprocess.Popen(["xfconf-query",
                                  "-c",
                                  "xfce4-desktop",
                                  "-p",
                                  "/backdrop",
                                  "-l"],
                                 stdout=subprocess.PIPE)
    props = read_prop.stdout.read().decode("utf-8").split("\n")
    for prop in props:
        for monitor, imgname in zip(monitors, img_names):
            if monitor in prop:
                if "last-image" in prop or "image-path" in prop:
                    os.system(
                        "xfconf-query -c xfce4-desktop -p "
                        + prop
                        + " -s ''")
                    os.system(
                        "xfconf-query -c xfce4-desktop -p "
                        + prop
                        + " -s '%s'" % imgname)
                if "image-show" in prop:
                    os.system(
                        "xfconf-query -c xfce4-desktop -p "
                        + prop
                        + " -s 'true'")
    # Delete old images after new ones are set
    if outputfile:
        remove_old_temp_files(outputfile)


def change_wallpaper_job(profile):
    """Centralized wallpaper method that calls setter algorithm based on input prof settings."""
    with G_WALLPAPER_CHANGE_LOCK:
        if profile.spanmode.startswith("single") and profile.ppimode is False:
            thrd = Thread(target=span_single_image_simple, args=(profile,), daemon=True)
            thrd.start()
        elif profile.spanmode.startswith("single") and profile.ppimode is True:
            thrd = Thread(target=span_single_image_advanced, args=(profile,), daemon=True)
            thrd.start()
        elif profile.spanmode.startswith("multi"):
            thrd = Thread(target=set_multi_image_wallpaper, args=(profile,), daemon=True)
            thrd.start()
        else:
            sp_logging.G_LOGGER.info("Unkown profile spanmode: %s", profile.spanmode)
        return thrd


def run_profile_job(profile):
    """This method executes the input profile as the profile is configured."""
    repeating_timer = None
    get_display_data()  # Check here so new profile has fresh data.
    if sp_logging.DEBUG:
        sp_logging.G_LOGGER.info("running profile job with profile: %s", profile.name)

    if not profile.slideshow:
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("Running a one-off wallpaper change.")
        change_wallpaper_job(profile)
    elif profile.slideshow:
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("Running wallpaper slideshow.")
        change_wallpaper_job(profile)
        repeating_timer = RepeatedTimer(
            profile.delay_list[0], change_wallpaper_job, profile)
    return repeating_timer


def quick_profile_job(profile):
    """
    At startup and profile change, switch to old temp wallpaper.

    Since the image processing takes some time, in order to carry
    out actions quickly at startup or at user request, set the old
    temp image of the requested profile as the wallpaper.
    """
    with G_WALLPAPER_CHANGE_LOCK:
        # Look for old temp image:
        files = [i for i in os.listdir(TEMP_PATH)
                 if os.path.isfile(os.path.join(TEMP_PATH, i))
                 and i.startswith(profile.name + "-")]
        if sp_logging.DEBUG:
            sp_logging.G_LOGGER.info("quickswitch file lookup: %s", files)
        if files:
            image_pieces = [os.path.join(TEMP_PATH, i) for i in files
                            if "-crop-" in i]
            if use_image_pieces() and image_pieces:
                image_pieces.sort()
                if sp_logging.DEBUG:
                    sp_logging.G_LOGGER.info("Use wallpaper crop pieces: %s",
                                             image_pieces)
                thrd = Thread(target=set_wallpaper_piecewise,
                            args=(image_pieces ,),
                            daemon=True)
                thrd.start()
            else:
                thrd = Thread(target=set_wallpaper,
                            args=(os.path.join(TEMP_PATH, files[0]),),
                            daemon=True)
                thrd.start()
        else:
            if sp_logging.DEBUG:
                sp_logging.G_LOGGER.info("Old file for quickswitch was not found. %s",
                              files)

def use_image_pieces():
    """Determine if it improves perfomance to use existing image pieces.
    
    Systems that use image pieces are: KDE, XFCE.
    """
    pltform = platform.system()
    if pltform == "Linux":
        desk_env = os.environ.get("DESKTOP_SESSION")
        if desk_env in ["/usr/share/xsessions/plasma", "plasma"]:
            return True
        elif desk_env in ["xfce", "xubuntu"]:
            return True
        else:
            return False
    else:
        return False