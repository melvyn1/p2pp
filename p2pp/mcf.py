__author__ = 'Tom Van den Eede'
__copyright__ = 'Copyright 2018-2020, Palette2 Splicer Post Processing Project'
__credits__ = ['Tom Van den Eede',
               'Tim Brookman'
               ]
__license__ = 'GPLv3'
__maintainer__ = 'Tom Van den Eede'
__email__ = 'P2PP@pandora.be'

import os
import re
import time

import p2pp.gcode as gcode
import p2pp.gui as gui
import p2pp.p2_m4c as m4c
import p2pp.parameters as parameters
import p2pp.pings as pings
import p2pp.purgetower as purgetower
import p2pp.variables as v
from p2pp.gcodeparser import parse_slic3r_config
from p2pp.omega import header_generate_omega, algorithm_process_material_configuration
from p2pp.sidewipe import create_side_wipe, create_sidewipe_bb3d

layer_regex = re.compile(";\s*LAYER\s+(\d+)\s*")
layerheight_regex = re.compile(";\s*LAYERHEIGHT\s+(\d+(\.\d+)?)\s*")


def optimize_tower_skip(skipmax, layersize):
    skipped = 0.0
    skipped_num = 0

    if v.side_wipe or v.bigbrain3d_purge_enabled:
        base = -1
    else:
        base = 0

    for idx in range(len(v.skippable_layer) - 1, base, -1):
        if skipped + 0.005 >= skipmax:
            v.skippable_layer[idx] = False
        elif v.skippable_layer[idx]:
            skipped = skipped + layersize
            skipped_num += 1

    if v.tower_delta:
        if skipped > 0:
            gui.log_warning(
                "Warning: Purge Tower delta in effect: {} Layers or {:-6.2f}mm".format(skipped_num, skipped))
        else:
            gui.create_logitem("Tower Purge Delta could not be applied to this print")
            for idx in range(len(v.skippable_layer)):
                v.skippable_layer[idx] = False
            v.tower_delta = False

    if not v.side_wipe and not v.bigbrain3d_purge_enabled:
        v.skippable_layer[0] = False


# ################### GCODE PROCESSING ###########################
def gcode_process_toolchange(new_tool, location, current_layer):
    # some commands are generated at the end to unload filament,
    # they appear as a reload of current filament - messing up things
    if new_tool == v.current_tool:
        return

    location += v.splice_offset

    if new_tool == -1:
        location += v.extra_runout_filament
        v.material_extruded_per_color[v.current_tool] += v.extra_runout_filament
        v.total_material_extruded += v.extra_runout_filament
    else:
        v.palette_inputs_used[new_tool] = True

    length = location - v.previous_toolchange_location

    if v.current_tool != -1:

        v.splice_extruder_position.append(location)
        v.splice_length.append(length)
        v.splice_used_tool.append(v.current_tool)

        v.autoadded_purge = 0

        if len(v.splice_extruder_position) == 1:
            if v.splice_length[0] < v.min_start_splice_length:
                if v.autoaddsplice and (v.full_purge_reduction or v.side_wipe):
                    v.autoadded_purge = v.min_start_splice_length - length
                else:
                    gui.log_warning("Warning : Short first splice (<{}mm) Length:{:-3.2f}".format(length,
                                                                                                  v.min_start_splice_length))

                    filamentshortage = v.min_start_splice_length - v.splice_length[0]
                    v.filament_short[new_tool] = max(v.filament_short[new_tool], filamentshortage)
        else:
            if v.splice_length[-1] < v.min_splice_length:
                if v.autoaddsplice and (v.full_purge_reduction or v.side_wipe):
                    v.autoadded_purge = v.min_splice_length - v.splice_length[-1]
                else:
                    gui.log_warning("Warning: Short splice (<{}mm) Length:{:-3.2f} Layer:{} Input:{}".
                                    format(v.min_splice_length, length, current_layer, v.current_tool + 1))
                    filamentshortage = v.min_splice_length - v.splice_length[-1]
                    v.filament_short[new_tool] = max(v.filament_short[new_tool], filamentshortage)

        v.side_wipe_length += v.autoadded_purge
        v.splice_extruder_position[-1] += v.autoadded_purge
        v.splice_length[-1] += v.autoadded_purge

        v.previous_toolchange_location = v.splice_extruder_position[-1]

    v.previous_tool = v.current_tool
    v.current_tool = new_tool


def inrange(number, low, high):
    if number is None:
        return True
    return low <= number <= high


def y_on_bed(y):
    return inrange(y, v.bed_origin_y, v.bed_origin_y + v.bed_size_y)


def x_on_bed(x):
    return inrange(x, v.bed_origin_x, v.bed_origin_x + v.bed_size_x)


def coordinate_on_bed(x, y):
    return x_on_bed(x) and y_on_bed(y)


def x_coordinate_in_tower(x):
    if x is None:
        return False
    return inrange(x, v.wipe_tower_info['minx'], v.wipe_tower_info['maxx'])


def y_coordinate_in_tower(y):
    if y is None:
        return False
    return inrange(y, v.wipe_tower_info['miny'], v.wipe_tower_info['maxy'])


def coordinate_in_tower(x, y):
    return x_coordinate_in_tower(x) and y_coordinate_in_tower(y)


def check_move_in_tower(x, y):
    if x and y:
        return coordinate_in_tower(x, y)
    return False


def entertower(layer_hght):
    purgeheight = layer_hght - v.cur_tower_z_delta
    if v.current_position_z != purgeheight:
        v.max_tower_delta = max(v.cur_tower_z_delta, v.max_tower_delta)
        gcode.issue_code(";------------------------------\n")
        gcode.issue_code(";  P2PP DELTA ENTER\n")
        gcode.issue_code(
            ";  Current Z-Height = {:.2f};  Tower height = {:.2f}; delta = {:.2f} [ {} ]".format(v.current_position_z,
                                                                                                 purgeheight,
                                                                                                 v.current_position_z - purgeheight,
                                                                                                 layer_hght))
        if v.retraction >= 0:
            purgetower.retract(v.current_tool)
        gcode.issue_code(
            "G1 Z{:.2f} F10810\n".format(purgeheight))

        # purgetower.unretract(v.current_tool)

        gcode.issue_code(";------------------------------\n")
        if purgeheight <= 0.21:
            gcode.issue_code("G1 F{}\n".format(min(1200, v.wipe_feedrate)))
        else:
            gcode.issue_code("G1 F{}\n".format(v.wipe_feedrate))


def leavetower():
    gcode.issue_code(";------------------------------\n")
    gcode.issue_code(";  P2PP DELTA LEAVE\n")
    gcode.issue_code(
        ";  Returning to Current Z-Height = {:.2f}; ".format(v.current_position_z))
    gcode.issue_code(
        "G1 Z{:.2f} F10810\n".format(v.current_position_z))
    gcode.issue_code(";------------------------------\n")


CLS_UNDEFINED = 0
CLS_NORMAL = 1
CLS_TOOL_START = 2
CLS_TOOL_UNLOAD = 4
CLS_TOOL_PURGE = 8
CLS_EMPTY = 16
CLS_BRIM = 32
CLS_BRIM_END = 64
CLS_ENDGRID = 128
CLS_COMMENT = 256
CLS_ENDPURGE = 512
CLS_TONORMAL = 1024
CLS_TOOLCOMMAND = 2048

hash_FIRST_LAYER_BRIM_START = hash("; CP WIPE TOWER FIRST LAYER BRIM START")
hash_FIRST_LAYER_BRIM_END = hash("; CP WIPE TOWER FIRST LAYER BRIM END")
hash_EMPTY_GRID_START = hash("; CP EMPTY GRID START")
hash_EMPTY_GRID_END = hash("; CP EMPTY GRID END")
hash_TOOLCHANGE_START = hash("; CP TOOLCHANGE START")
hash_TOOLCHANGE_UNLOAD = hash("; CP TOOLCHANGE UNLOAD")
hash_TOOLCHANGE_WIPE = hash("; CP TOOLCHANGE WIPE")
hash_TOOLCHANGE_END = hash("; CP TOOLCHANGE END")


def update_class(gcode_line):

    line_hash = hash(gcode_line)

    if line_hash == hash_EMPTY_GRID_START:
        v.block_classification = CLS_EMPTY
        v.layer_emptygrid_counter += 1
        return

    if line_hash == hash_EMPTY_GRID_END:
        v.block_classification = CLS_ENDGRID
        return

    if line_hash == hash_TOOLCHANGE_START:
        v.block_classification = CLS_TOOL_START
        v.layer_toolchange_counter += 1
        return

    if line_hash == hash_TOOLCHANGE_UNLOAD:
        v.block_classification = CLS_TOOL_UNLOAD
        return

    if line_hash == hash_TOOLCHANGE_WIPE:
        v.block_classification = CLS_TOOL_PURGE
        return

    if line_hash == hash_TOOLCHANGE_END:
        if v.previous_block_classification == CLS_TOOL_UNLOAD:
            v.block_classification = CLS_NORMAL
        else:
            if v.previous_block_classification == CLS_TOOL_PURGE:
                v.block_classification = CLS_ENDPURGE
            else:
                v.block_classification = CLS_TONORMAL
        return

    if line_hash == hash_FIRST_LAYER_BRIM_START:
        v.block_classification = CLS_BRIM
        v.tower_measure = True
        return

    if line_hash == hash_FIRST_LAYER_BRIM_END:
        v.tower_measure = False
        v.block_classification = CLS_BRIM_END
        return


def calculate_tower(x, y):
    if x is not None:
        v.wipe_tower_info['minx'] = min(v.wipe_tower_info['minx'], x - 2 * v.extrusion_width)
        v.wipe_tower_info['maxx'] = max(v.wipe_tower_info['maxx'], x + 2 * v.extrusion_width)
    if y is not None:
        v.wipe_tower_info['miny'] = min(v.wipe_tower_info['miny'], y - 4 * v.extrusion_width)
        v.wipe_tower_info['maxy'] = max(v.wipe_tower_info['maxy'], y + 4 * v.extrusion_width)


def create_tower_gcode():
    # generate a purge tower alternative
    _x = v.wipe_tower_info['minx']
    _y = v.wipe_tower_info['miny']
    _w = v.wipe_tower_info['maxx'] - v.wipe_tower_info['minx']
    _h = v.wipe_tower_info['maxy'] - v.wipe_tower_info['miny']

    purgetower.purge_create_layers(_x, _y, _w, _h)
    # generate og items for the new purge tower
    gui.create_logitem(
        " Purge Tower :Loc X{:.2f} Y{:.2f}  W{:.2f} H{:.2f}".format(_x, _y, _w, _h))
    gui.create_logitem(
        " Layer Length Solid={:.2f}mm   Sparse={:.2f}mm".format(purgetower.sequence_length_solid,
                                                                purgetower.sequence_length_empty))


def parse_gcode():

    v.layer_toolchange_counter = 0
    v.layer_emptygrid_counter = 0

    v.block_classification = CLS_NORMAL
    v.previous_block_classification = CLS_NORMAL
    total_line_count = len(v.input_gcode)

    backpass_line = -1

    for index in range(total_line_count):

        v.previous_block_classification = v.block_classification

        if index % 5000 == 0:
            gui.progress_string(4 + 46 * index // total_line_count)

        line = v.input_gcode[index]
        is_comment = False

        if line.startswith(';'):

            # try finding a P2PP configuration command
            # until config_end command is encountered

            is_comment = True

            if line.startswith('; CP'):
                update_class(line)
            else:

                layer = -1
                # if not supports are printed or layers are synced, there is no need to look at the layerheight,
                # otherwise look at the layerheight to determine the layer progress

                if v.synced_support or not v.support_material:
                    lm = layer_regex.match(line)
                    if lm:
                        layer = int(lm.group(1))
                else:
                    lm = layerheight_regex.match(line)
                    if lm:
                        fl = int(v.first_layer_height * 100)
                        lv = int((float(lm.group(1)) + 0.001) * 100)
                        lv = lv - fl

                        lh = int(v.layer_height * 100)

                        if lv % lh == 0:
                            layer = int(lv / lh)
                        else:
                            layer = v.last_parsed_layer

                if lm:
                    if layer >= 0 and layer != v.last_parsed_layer:
                        v.last_parsed_layer = layer
                        v.layer_end.append(index)
                        if layer > 0:
                            v.skippable_layer.append((v.layer_emptygrid_counter > 0) and (v.layer_toolchange_counter == 0))
                            v.layer_toolchange_counter = 0
                            v.layer_emptygrid_counter = 0
                else:
                    if not v.p2pp_configend or v.set_tool > 3 or v.p2pp_tool_unconfigged[v.set_tool]:
                        m = v.regex_p2pp.match(line)
                        if m:
                            if m.group(1).startswith("MATERIAL"):
                                algorithm_process_material_configuration(m.group(1)[9:])
                            else:
                                parameters.check_config_parameters(m.group(1), m.group(2))

        else:
            try:
                if line[0] == 'T':
                    v.block_classification = CLS_TOOL_PURGE
                    cur_tool = int(line[1])
                    if v.set_tool != -1 and v.set_tool < 4:
                        v.p2pp_tool_unconfigged[v.set_tool] = False
                    v.set_tool = cur_tool
                    v.m4c_toolchanges.append(cur_tool)
                    v.m4c_toolchange_source_positions.append(len(v.parsed_gcode))
            except (TypeError, IndexError):
                pass

        code = gcode.GCodeCommand(line, is_comment)
        code.Class = v.block_classification
        v.parsed_gcode.append(code)

        if v.block_classification != v.previous_block_classification:

            if v.block_classification == CLS_BRIM or \
                    v.block_classification == CLS_TOOL_START or \
                    v.block_classification == CLS_TOOL_UNLOAD or \
                    v.block_classification == CLS_EMPTY:
                idx = backpass_line
                while idx < len(v.parsed_gcode):
                    v.parsed_gcode[idx].Class = v.block_classification
                    idx += 1

        if v.tower_measure:
            calculate_tower(code.X, code.Y)
        else:
            if code.is_movement_command and check_move_in_tower(code.X, code.Y):
                backpass_line = len(v.parsed_gcode)-1

        if v.block_classification == CLS_ENDGRID or v.block_classification == CLS_ENDPURGE:
            if code.X and code.Y:
                if not coordinate_in_tower(code.X, code.Y):
                    v.parsed_gcode[-1].Class = CLS_NORMAL
                    v.block_classification = CLS_NORMAL

        if v.block_classification == CLS_BRIM_END:
            v.block_classification = CLS_NORMAL


def gcode_parseline(index):

    g = v.parsed_gcode[index]

    try:
        if index >= v.layer_end[0]:
            v.last_parsed_layer += 1
            v.layer_end.pop(0)
    except IndexError:
        pass

    if g.Command is None:
        g.issue_command()
        return

    if not g.is_movement_command:

        if g.Command.startswith('T'):
            gcode_process_toolchange(int(g.command_value()), v.total_material_extruded, v.last_parsed_layer)
            if not v.debug_leaveToolCommands:
                g.move_to_comment("Color Change")
            v.toolchange_processed = True
        else:

            if g.Command in ["M140", "M190", "M73", "M84", "M201", "M203", "G28", "G90", "M115", "G80", "G21", "M907", "M204"]:
                g.issue_command()
                return

            if g.Command in ["M104", "M109"]:
                if not v.process_temp or g.Class not in [CLS_TOOL_PURGE, CLS_TOOL_START, CLS_TOOL_UNLOAD]:
                    g.add_comment(" Unprocessed temp ")
                    g.issue_command()
                    v.new_temp = g.get_parameter("S", v.current_temp)
                    v.current_temp = v.new_temp
                else:
                    v.new_temp = g.get_parameter("S", v.current_temp)
                    if v.new_temp >= v.current_temp:
                        g.Command = "M109"
                        v.temp2_stored_command = g.__str__()
                        g.move_to_comment("delayed temp rise until after purge {}-->{}".format(v.current_temp, v.new_temp))
                        v.current_temp = v.new_temp
                    else:
                        v.temp1_stored_command = g.__str__()
                        g.move_to_comment("delayed temp drop until after purge {}-->{}".format(v.current_temp, v.new_temp))
                        g.issue_command()
                return

            if g.Command == "M107":
                g.issue_command()
                v.saved_fanspeed = 0
                return

            if g.Command == "M106":
                g.issue_command()
                v.saved_fanspeed = g.get_parameter("S", v.saved_fanspeed)
                return

            # flow rate changes have an effect on the filament consumption.  The effect is taken into account for ping generation
            if g.Command == "M221":
                v.extrusion_multiplier = float(g.get_parameter("S", v.extrusion_multiplier * 100)) / 100
                g.issue_command()
                return

            # feed rate changes in the code are removed as they may interfere with the Palette P2 settings
            if g.Command in ["M220"]:
                g.move_to_comment("Feed Rate Adjustments are removed")
                g.issue_command()
                return

            if g.Class == CLS_TOOL_UNLOAD:
                if g.Command == "G4" or (g.Command in ["M900"] and g.get_parameter("K", 0) == 0):
                    g.move_to_comment("tool unload")

            g.issue_command()
            return

    previous_block_class = v.parsed_gcode[max(0, index - 1)].Class
    classupdate = g.Class != previous_block_class

    if not (g.Class in [CLS_TOOL_PURGE, CLS_TOOL_START, CLS_TOOL_UNLOAD]) and v.current_temp != v.new_temp:
        gcode.issue_code(v.temp1_stored_command)
        v.temp1_stored_command = ""

    # ---- AS OF HERE ONLY MOVEMENT COMMANDS ----

    v.keep_speed = g.get_parameter("F", v.keep_speed)

    if g.X:
        v.previous_purge_keep_x = v.purge_keep_x
        v.purge_keep_x = g.X
        if x_coordinate_in_tower(g.X):
            v.keep_x = g.X

    if g.Y:
        v.previous_purge_keep_y = v.purge_keep_y
        v.purge_keep_y = g.Y
        if y_coordinate_in_tower(g.Y):
            v.keep_y = g.Y

    if classupdate:

        if g.Class in [CLS_TOOL_PURGE, CLS_EMPTY]:
            v.purge_count = 0

        if g.Class == CLS_BRIM and v.side_wipe and v.bigbrain3d_purge_enabled:
            v.side_wipe_length = v.bigbrain3d_prime * v.bigbrain3d_blob_size
            create_sidewipe_bb3d()

    if g.Class in [CLS_TOOL_START, CLS_TOOL_UNLOAD]:
        if v.side_wipe or v.tower_delta or v.full_purge_reduction:
            g.move_to_comment("tool unload")
        else:
            if g.Z:
                g.remove_x()
                g.remove_y()
                g.remove_f()
                g.remove_e()
            else:
                g.move_to_comment("tool unload")
        g.issue_command()
        return

    if g.Class == CLS_TOOL_PURGE and not (v.side_wipe or v.full_purge_reduction) and g.E:
        _x = g.get_parameter("X", v.current_position_x)
        _y = g.get_parameter("Y", v.current_position_y)
        # remove positive extrusions while moving into the tower
        if not (coordinate_in_tower(_x, _y) and coordinate_in_tower(v.purge_keep_x, v.purge_keep_y)) and g.E > 0:
            g.remove_e()

    if not v.full_purge_reduction and not v.side_wipe and g.E and g.has_parameter("F"):
        if v.keep_speed > v.purgetopspeed:
            g.update_parameter("F", v.purgetopspeed)
            g.add_comment(" prugespeed topped")

    if v.side_wipe:
        _x = g.X if g.X else v.current_position_x
        _y = g.Y if g.Y else v.current_position_y
        if not coordinate_on_bed(_x, _y):
            g.remove_x()
            g.remove_y()

    # pathprocessing = sidewipe, fullpurgereduction or tower delta

    if v.pathprocessing:

        if g.Class == CLS_TONORMAL and not g.is_comment():
            g.move_to_comment("post block processing")
            g.issue_command()
            return

        # remove any commands that are part of the purge tower and still perofrm actions WITHIN the tower
        if g.Class in [CLS_ENDPURGE, CLS_ENDGRID]:
            if check_move_in_tower(g.X, g.Y):
                g.remove_x()
                g.remove_y()

        # sepcific for FULL_PURGE_REDUCTION
        if v.full_purge_reduction:

            if g.Class == CLS_BRIM_END:
                create_tower_gcode()
                purgetower.purge_generate_brim()

        # sepcific for SIDEWIPE
        if v.side_wipe:

            # side wipe does not need a brim
            if g.Class == CLS_BRIM:
                g.move_to_comment("side wipe - removed")
                g.issue_command()
                return
        else:
            if classupdate and g.Class == CLS_TOOL_PURGE:
                g.issue_command()
                gcode.issue_code("G1 X{} Y{} F8640;\n".format(v.keep_x, v.keep_y))
                v.current_position_x = v.keep_x
                v.current_position_x = v.keep_y

        # specific for TOWER DELTA
        if v.tower_delta:
            if classupdate:
                if g.Class == CLS_TOOL_PURGE:
                    entertower(v.last_parsed_layer * v.layer_height + v.first_layer_height)
                    return

                if previous_block_class == CLS_TOOL_PURGE:
                    leavetower()

        # if path processing is on then detect moevement into the tower
        # since there is no empty path processing, it must be beginning of the
        # empty grid sequence.

        if not v.towerskipped:
            try:
                v.towerskipped = v.skippable_layer[v.last_parsed_layer] and check_move_in_tower(g.X, g.Y)
                if v.towerskipped and v.tower_delta:
                    v.cur_tower_z_delta += v.layer_height
                    gcode.issue_code(";-------------------------------------\n")
                    gcode.issue_code(";  GRID SKIP --TOWER DELTA {:6.2f}mm\n".format(v.cur_tower_z_delta))
                    gcode.issue_code(";-------------------------------------\n")
            except IndexError:
                pass

        # EMPTY GRID SKIPPING CHECK FOR SIDE WIPE/TOWER DELTA/FULLPURGE
        if g.Class == CLS_EMPTY and "EMPTY GRID START" in g.get_comment():
            if not v.side_wipe and v.last_parsed_layer >= len(v.skippable_layer) or not v.skippable_layer[v.last_parsed_layer]:
                entertower(v.last_parsed_layer * v.layer_height + v.first_layer_height)

        # changing from EMPTY to NORMAL
        ###############################
        if (previous_block_class == CLS_ENDGRID) and (g.Class == CLS_NORMAL):
            v.towerskipped = False

        if v.towerskipped:
            # keep retracts
            if not g.is_comment():
                if g.is_retract_command():
                    if v.retraction <= - (v.retract_length[v.current_tool] - 0.02):
                        g.move_to_comment("tower skipped//Double Retract")
                    else:
                        if g.E:
                            v.retraction += g.E
                        else:
                            v.retraction -= 1
                else:
                    if not g.Z:
                        g.move_to_comment("tower skipped")
            g.issue_command()
            return

        if v.tower_delta:
            if g.E and g.Class in [CLS_TOOL_UNLOAD, CLS_TOOL_PURGE]:
                if not inrange(g.X, v.wipe_tower_info['minx'], v.wipe_tower_info['maxx']):
                    g.remove_e()
                if not inrange(g.Y, v.wipe_tower_info['miny'], v.wipe_tower_info['maxy']):
                    g.remove_e()

        if v.full_purge_reduction and g.Class == CLS_NORMAL and classupdate:
            purgetower.purge_generate_sequence()

    else:

        if classupdate and g.Class in [CLS_TOOL_PURGE, CLS_EMPTY]:

            if v.acc_ping_left <= 0:
                pings.check_accessorymode_first()
            v.enterpurge = True

        if v.enterpurge and g.is_movement_command:

            v.enterpurge = False

            _x = v.previous_purge_keep_x if g.X else v.purge_keep_x
            _y = v.previous_purge_keep_y if g.Y else v.purge_keep_y

            if not coordinate_in_tower(_x, _y):
                _x = v.purge_keep_x
                _y = v.purge_keep_y

            if v.retraction == 0:
                purgetower.retract(v.current_tool, 3000)

            if v.temp2_stored_command != "":

                x_offset = 2 + 4 * v.extrusion_width
                y_offset = 2 + 8 * v.extrusion_width

                if abs(v.wipe_tower_info['minx'] - v.purge_keep_x) < abs(v.wipe_tower_info['maxx'] - v.purge_keep_x):
                    v.current_position_x = v.wipe_tower_info['minx'] + x_offset
                else:
                    v.current_position_x = v.wipe_tower_info['maxx'] - x_offset

                if abs(v.wipe_tower_info['miny'] - v.purge_keep_y) < abs(v.wipe_tower_info['maxy'] - v.purge_keep_y):
                    v.current_position_y = v.wipe_tower_info['miny'] + y_offset
                else:
                    v.current_position_y = v.wipe_tower_info['maxy'] - y_offset

                gcode.issue_code(
                    "G1 X{:.3f} Y{:.3f} F8640; Move outside of tower to prevent ooze problems\n".format(
                        v.current_position_x, v.current_position_y))

                gcode.issue_code(v.temp2_stored_command)
                v.temp2_stored_command = ""

            gcode.issue_code(
                "G1 X{:.3f} Y{:.3f} F8640; P2PP Inserted to realign\n".format(v.purge_keep_x, v.purge_keep_y))
            v.current_position_x = _x
            v.current_position_x = _y

            g.remove_e()
            if g.get_parameter("X") == _x:
                g.remove_x()
            if len(g.Parameters) == 0:
                g.move_to_comment("-useless command-")

    if v.expect_retract and (g.X or g.Y):
        if not v.retraction < 0:
            if g.E and g.E < 0:
                purgetower.retract(v.current_tool)
        v.expect_retract = False

    if v.retract_move and g.is_retract_command():
        # This is going to break stuff, G10 cannot take X and Y, what to do?
        g.update_parameter("X", v.retract_x)
        g.update_parameter("Y", v.retract_y)
        v.retract_move = False

    v.current_position_x = g.X if g.X else v.current_position_x
    v.current_position_y = g.Y if g.Y else v.current_position_y
    v.current_position_z = g.Z if g.Z else v.current_position_z

    if g.Class == CLS_BRIM and v.full_purge_reduction:
        g.move_to_comment("replaced by P2PP brim code")
        g.remove_e()

    if v.side_wipe or v.full_purge_reduction:
        if g.Class in [CLS_TOOL_PURGE, CLS_ENDPURGE, CLS_EMPTY]:
            if v.last_parsed_layer < len(v.skippable_layer) and v.skippable_layer[v.last_parsed_layer]:
                g.move_to_comment("skipped purge")
            else:
                if g.E:
                    v.side_wipe_length += g.E
                g.move_to_comment("side wipe/full purge")

    if v.toolchange_processed:
        if v.side_wipe and g.Class == CLS_NORMAL and classupdate:
            if v.bigbrain3d_purge_enabled:
                create_sidewipe_bb3d()
            else:
                create_side_wipe()
            v.toolchange_processed = False

        if g.Class == CLS_NORMAL:
            gcode.GCodeCommand(";TOOLCHANGE PROCESSED").issue_command()
            v.toolchange_processed = False

    # check here issue with unretract
    #################################

    # g.Comment = " ; - {}".format(v.total_material_extruded)

    if g.is_retract_command():
        if v.retraction <= - (v.retract_length[v.current_tool] - 0.02):
            g.move_to_comment("Double Retract")
        else:
            if g.E:
                v.retraction += g.E
            else:
                v.retraction -= 1

    if g.is_unretract_command():
        if g.E:
            g.update_parameter("E", min(-v.retraction, g.E))
            v.retraction += g.E
        else:
            v.retraction = 0

    if (g.X or g.Y) and (g.E and g.E > 0) and v.retraction < 0 and abs(v.retraction) > 0.01:
        gcode.issue_code(";fixup retracts\n")
        purgetower.unretract(v.current_tool)
        # v.retracted = False

    g.issue_command()

    # PING PROCESSING

    if v.accessory_mode:
        pings.check_accessorymode_second(g.E)

    if (g.E and g.E > 0) and v.side_wipe_length == 0:
        pings.check_connected_ping()

    v.previous_position_x = v.current_position_x
    v.previous_position_y = v.current_position_y


# Generate the file and glue it all together!

def generate(input_file, output_file, printer_profile, splice_offset, silent):
    starttime = time.time()
    v.printer_profile_string = printer_profile
    basename = os.path.basename(input_file)
    _taskName = os.path.splitext(basename)[0].replace(" ", "_")
    _taskName = _taskName.replace(".mcf", "")

    v.splice_offset = splice_offset

    try:
        # python 3.x
        opf = open(input_file, encoding='utf-8')
    except TypeError:
        try:
            # python 2.x
            opf = open(input_file)
        except IOError:
            if v.gui:
                gui.user_error("P2PP - Error Occurred", "Could not read input file\n'{}'".format(input_file))
            else:
                print ("Could not read input file\n'{}".format(input_file))
            return
    except IOError:
        if v.gui:
            gui.user_error("P2PP - Error Occurred", "Could not read input file\n'{}'".format(input_file))
        else:
            print ("Could not read input file\n'{}".format(input_file))
        return

    gui.setfilename(input_file)
    gui.set_printer_id(v.printer_profile_string)
    gui.create_logitem("Reading File " + input_file)
    gui.progress_string(1)

    v.input_gcode = opf.readlines()
    opf.close()

    v.input_gcode = [item.strip() for item in v.input_gcode]

    gui.create_logitem("Analyzing slicer parameters")
    gui.progress_string(2)
    parse_slic3r_config()

    gui.create_logitem("Pre-parsing GCode")
    gui.progress_string(4)
    parse_gcode()

    if v.bed_size_x == -9999 or v.bed_size_y == -9999 or v.bed_origin_x == -9999 or v.bed_origin_y == -9999:
        gui.log_warning("Bedsize not correctly defined.  The generated file will NOT print")
    else:
        gui.create_logitem("Bed origin ({:3.1f}mm, {:3.1f}mm)".format(v.bed_origin_x, v.bed_origin_y))
        gui.create_logitem("Bed zise   ({:3.1f}mm, {:3.1f}mm)".format(v.bed_size_x, v.bed_size_y))
        if v.bed_shape_rect and v.bed_shape_warning:
            gui.create_logitem("Manual bed size override, Prusa Bedshape parameters ignored.")

    gui.create_logitem("")

    if v.save_unprocessed:
        pre, ext = os.path.splitext(input_file)
        of = pre + "_unprocessed" + ext
        gui.create_logitem("Outputing original code to: " + of)
        opf = open(of, "w")
        opf.writelines(v.input_gcode)
        opf.close()

    if v.tower_delta or v.full_purge_reduction:
        if v.variable_layer:
            gui.log_warning("Variable layers are not compatible with fullpruge/tower delta")

    if v.process_temp and v.side_wipe:
        gui.log_warning("TEMPERATURECONTROL and Side Wipe / BigBrain3D are not compatible")

    if v.palette_plus:
        if v.palette_plus_ppm == -9:
            gui.log_warning("P+ parameter P+PPM not set correctly in startup GCODE")
        if v.palette_plus_loading_offset == -9:
            gui.log_warning("P+ parameter P+LOADINGOFFSET not set correctly in startup GCODE")

    v.side_wipe = not coordinate_on_bed(v.wipetower_posx, v.wipetower_posy)
    v.tower_delta = v.max_tower_z_delta > 0

    gui.create_logitem("Creating tool usage information")
    m4c.calculate_loadscheme()

    if v.side_wipe:

        if v.skirts and v.ps_version > "2.2":
            gui.log_warning("SIDEWIPE and SKIRTS are NOT compatible in PS2.2 or later")
            gui.log_warning("THIS FILE WILL NOT PRINT CORRECTLY")

        if v.wipe_remove_sparse_layers:
            gui.log_warning("SIDE WIPE mode not compatible with sparse wipe tower in PS")
            gui.log_warning("THIS FILE WILL NOT PRINT CORRECTLY")

        gui.create_logitem("Side wipe activated", "blue")
        if v.full_purge_reduction:
            gui.log_warning("Full Purge Reduction is not compatible with Side Wipe, performing Side Wipe")
            v.full_purge_reduction = False

    if v.full_purge_reduction:
        v.side_wipe = False
        gui.create_logitem("Full Tower Reduction activated", "blue")
        if v.tower_delta:
            gui.log_warning("Full Purge Reduction is not compatible with Tower Delta, performing Full Purge Reduction")
            v.tower_delta = False

    v.pathprocessing = (v.tower_delta or v.full_purge_reduction or v.side_wipe)

    if v.autoaddsplice and not v.full_purge_reduction and not v.side_wipe:
        gui.log_warning("AUTOADDPURGE only works with side wipe and fullpurgereduction at this moment")

    if (len(v.skippable_layer) == 0) and v.pathprocessing:
        gui.log_warning("LAYER configuration is missing. NO OUTPUT FILE GENERATED.")
        gui.log_warning("Check the P2PP documentation for furhter info.")
    else:

        if v.tower_delta:
            optimize_tower_skip(v.max_tower_z_delta, v.layer_height)

        if v.side_wipe:
            optimize_tower_skip(999, v.layer_height)

        gui.create_logitem("Generate processed GCode")

        total_line_count = len(v.input_gcode)
        v.retraction = 0
        v.last_parsed_layer = -1
        for process_line_count in range(total_line_count):
            gcode_parseline(process_line_count)
            gui.progress_string(50 + 50 * process_line_count // total_line_count)

        v.processtime = time.time() - starttime

        gcode_process_toolchange(-1, v.total_material_extruded, 0)
        omega_result = header_generate_omega(_taskName)
        header = omega_result['header'] + omega_result['summary'] + omega_result['warnings']

        # write the output file
        ######################

        if not output_file:
            output_file = input_file
        gui.create_logitem("Generating GCODE file: " + output_file)
        opf = open(output_file, "w")
        if not v.accessory_mode:
            opf.writelines(header)
            opf.write("\n\n;--------- START PROCESSED GCODE ----------\n\n")
        if v.accessory_mode:
            opf.write("M0\n")
            opf.write("T0\n")

        if v.splice_offset == 0:
            gui.log_warning("SPLICE_OFFSET not defined")
        opf.writelines(v.processed_gcode)
        opf.close()

        if v.accessory_mode:

            pre, ext = os.path.splitext(output_file)
            if v.palette_plus:
                maffile = pre + ".msf"
            else:
                maffile = pre + ".maf"
            gui.create_logitem("Generating PALETTE MAF/MSF file: " + maffile)

            maf = open(maffile, 'w')

            for h in header:
                h = h.strip('\r\n')
                maf.write(unicode(h))
                maf.write('\r\n')
            maf.close()
            #
            # with io.open(maffile, 'w', newline='\r\n') as maf:
            #
            #     for i in range(len(header)):
            #         h = header[i].strip('\n\r') + "\n"
            #         if not h.startswith(";"):
            #             try:
            #                 maf.write(unicode(h))
            #             except:
            #                 maf.write(h)

        gui.print_summary(omega_result['summary'])

    gui.progress_string(100)
    if (len(v.process_warnings) > 0 and not v.ignore_warnings) or v.consolewait:
        gui.close_button_enable()
