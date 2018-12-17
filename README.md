# p2pp
Palette2 Post Processing tool for Slic3r


Purpose:

Allow Palette 2 users to exploit all freatures and functionality of the palette 2 (Pro) with their Prusa devices, including

- use of variable layers without blowing up the wipe tower
- wipe to waste object
- wipe to infill

P2PP currently only works for devices in a connected setup.  It does not generqte the reauired sequences required to meet the Pause-based pings

Functionality:

P2PP works as a post processor to GCode files generated by Splic3r, just like Chroma does.   If does however not create any new code for wipe towers etc, it just adds a Palette 2 MCF header to the file and inserts Ping information in the Gcode at set intervals.  

Setup and Configuration

P2PP is a python script with a  shell script/batch file wrapper.  just put all in a folder of your choice and make sure the py and sh files are made executable when running on a unix/osx system.   The remainder of the configuration is done in Slic3r PE

Prior to using the script it is important to setup the printer according to the specifications set by Mosaic <LINK>

In addition to that the following setup is required to work P2PP

Add the Printer Profile ID and Splice Offset to the Printer Start GCode:

;Palette 2 Configuration 
;P2PP PRINTERPROFILE=0313be853ee2990c
;P2PP SPLICEOFFSET=30


For each piece of filament you need to include the followinf information
;P2PP FN=[filament_preset]
;P2PP FT=[filament_type]
;P2PP FC=[extruder_colour]

where for each type of filament with a different Splice profile you need to add a number.   Currently 4 are supported, but up to 9 can be defined through change in the script (work in progress)

