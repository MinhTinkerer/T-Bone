# coding=utf-8
from Adafruit_BBIO import PWM
from Queue import Queue, Empty
from copy import deepcopy
import logging
from math import copysign, sqrt
from threading import Thread

from numpy import sign
import time
import beagle_bone_pins
from heater import PwmHeater, Thermometer, PID, OnOffHeater

from machine import Machine, MAXIMUM_FREQUENCY_ACCELERATION, MAXIMUM_FREQUENCY_BOW
from helpers import convert_mm_to_steps, find_shortest_vector, calculate_relative_vector, \
    convert_velocity_clock_ref_to_realtime_ref, convert_acceleration_clock_ref_to_realtime_ref
from LEDS import LedManager

__author__ = 'marcus'
_logger = logging.getLogger(__name__)
_axis_config = {
    # maps axis name to config entry
    'x': 'x-axis',
    'y': 'y-axis',
    'z': 'z-axis',
    'e': 'extruder',
}
# order of the axis
_axis_names = ('x', 'y', 'z')


class Printer(Thread):
    def __init__(self, serial_port, reset_pin, print_queue_min_length=50, print_queue_max_length=100):
        Thread.__init__(self)
        self.ready = False
        self.printing = False
        self.config = None
        self.homed_axis = []

        self.heated_bed = None
        self.extruder_heater = None
        self.axis = {}

        self.axis_position = {}
        for axis_name in _axis_config:
            self.axis_position[axis_name] = 0

        self.printer_thread = None
        self._print_queue = None
        self.print_queue_min_length = print_queue_min_length
        self.print_queue_max_length = print_queue_max_length
        self._default_homing_retraction = None
        self._x_step_conversion = None
        self._y_step_conversion = None

        self._homing_timeout = 10
        self._print_queue_wait_time = 0.1
        self.homed = False

        self.led_manager = LedManager()

        # todo why didn't this work as global constant?? - should be confugired anyway
        self._FAN_OUTPUT = beagle_bone_pins.pwm_config[2]['out']

        # finally create the machine
        self.machine = Machine(serial_port=serial_port, reset_pin=reset_pin)
        self.running = True
        self.start()

    def stop(self):
        if self.running:
            self.running = False
        if self.isAlive():
            self.join()
        self.machine.disconnect()

    def axis_names(self):
        return _axis_names

    def configure(self, config):
        if not config:
            raise PrinterError("No printer config given!")

        self.config = config

        printer_config = config['printer']
        print_queue_config = printer_config["print-queue"]
        self.print_queue_min_length = print_queue_config['min-length']
        self.print_queue_max_length = print_queue_config['max-length']
        self._homing_timeout = printer_config['homing-timeout']
        self._default_homing_retraction = printer_config['home-retract']
        self.default_speed = printer_config['default-speed']

        # todo this is the fan and should be configured
        PWM.start(self._FAN_OUTPUT, printer_config['fan-duty-cycle'], printer_config['fan-frequency'], 0)

        if 'heated-bed' in printer_config:
            bed_heater_config = printer_config['heated-bed']
            self.heated_bed = self._configure_heater(bed_heater_config)

        extruder_heater_config = config['extruder']['heater']
        # we do not care if it the extruder heate may not be given in the config
        # # - the whole point of additive printing is pretty dull w/o an heated extruder
        self.extruder_heater = self._configure_heater(extruder_heater_config)

        for axis_name, config_name in _axis_config.iteritems():
            _logger.info("Configuring axis \'%s\' according to conf \'%s\'", axis_name, config_name)
            axis = {'name': axis_name}
            self.axis[axis_name] = axis
            self._configure_axis(axis, config[config_name])
        self._postconfig()

    def connect(self):
        _logger.debug("Connecting printer")
        self.machine.connect()


    def start_print(self):
        self._print_queue = PrintQueue(axis_config=self.axis, min_length=self.print_queue_min_length,
                                       max_length=self.print_queue_max_length, default_target_speed=self.default_speed)
        self.machine.start_motion()
        self.printing = True
        self.led_manager.light(1, True)


    def finish_print(self):
        self._print_queue.finish()
        self.machine.finish_motion()
        self.printing = False
        self.led_manager.light(1, False)

    def read_motor_positons(self):
        positions = {}
        for axis_name in self.axis:
            axis_config = self.axis[axis_name]
            motor = axis_config['motor']
            position = self.machine.read_positon(motor)
            positions[axis_name] = position / axis_config['steps_per_mm']
        return positions

    def read_axis_status(self):
        status = {}
        for axis_name in self.axis:
            axis_config = self.axis[axis_name]
            motor = axis_config['motor']
            if motor:
                internal_status = self.machine.read_axis_status(motor)
                position = internal_status['position']
                position = position / axis_config['steps_per_mm']
                encoder_pos = internal_status['encoder_pos']
                encoder_pos = encoder_pos / axis_config['steps_per_mm']
                left_endstop_ = internal_status['left_endstop']
                right_endstop_ = internal_status['right_endstop']
            else:
                # todo implement
                position = 0
                encoder_pos = 0
                left_endstop_ = False
                right_endstop_ = False
            status[axis_name] = {
                "position": position,
                "encoder_pos": encoder_pos,
                "left_endstop": left_endstop_,
                "right_endstop": right_endstop_
            }
        return status


    def home(self, axis):
        for home_axis in axis:
            if not self.axis[home_axis]['end-stops'] or not self.axis[home_axis]['end-stops']['left']:
                _logger.debug("Axis %s does not have endstops - or an left end stop, cannot home it.", home_axis)
            else:
                _logger.info("Homing axis \'%s\' to zero", home_axis)
                # read the homing config for the axis
                home_speed = self.axis[home_axis]['home_speed']
                home_precision_speed = self.axis[home_axis]['home_precision_speed']
                home_acceleration = self.axis[home_axis]['home_acceleration']
                home_retract = self.axis[home_axis]['home_retract']
                # TODO we just enforce the existence of a left endstop - is there a simpler way?
                if self.axis[home_axis]['end-stops']['left']['type'] == 'virtual':
                    homing_right_position = convert_mm_to_steps(self.axis[home_axis]['end-stops']['left']['distance']
                                                                , self.axis[home_axis]['steps_per_mm'])
                else:
                    homing_right_position = 0
                # convert everything from mm to steps
                home_speed = convert_mm_to_steps(home_speed, self.axis[home_axis]['steps_per_mm'])
                home_precision_speed = convert_mm_to_steps(home_precision_speed, self.axis[home_axis]['steps_per_mm'])
                home_acceleration = convert_mm_to_steps(home_acceleration, self.axis[home_axis]['steps_per_mm'])
                if self.axis[home_axis]['clock-referenced']:
                    home_speed = convert_velocity_clock_ref_to_realtime_ref(home_speed)
                    home_precision_speed = convert_velocity_clock_ref_to_realtime_ref(home_precision_speed)
                    home_acceleration = convert_acceleration_clock_ref_to_realtime_ref(home_acceleration)
                    # no home jerk - since this only applies to 5041 axis w/o jerk control
                home_retract = convert_mm_to_steps(home_retract, self.axis[home_axis]['steps_per_mm'])
                # make a config out of it
                if self.axis[home_axis]['motor']:
                    homing_config = {
                        'motor': self.axis[home_axis]['motor'],
                        'timeout': 0,
                        'home_speed': home_speed,
                        'home_slow_speed': home_precision_speed,
                        'home_retract': home_retract,
                        'acceleration': home_acceleration,
                        'homing_right_position': homing_right_position,
                    }
                    if self.axis[home_axis]['bow_step']:
                        homing_config['jerk'] = self.axis[home_axis]['bow_step']
                else:
                    # todo we should check if there is a motor for the left endstop??
                    homing_config = {
                        'motor': self.axis[home_axis]['end-stops']['left']['motor'],
                        'followers': self.axis[home_axis]['motors'],
                        'timeout': 0,
                        'home_speed': home_speed,
                        'home_slow_speed': home_precision_speed,
                        'home_retract': home_retract,
                        'acceleration': home_acceleration,
                        'homing_right_position': homing_right_position,
                    }
                    if self.axis[home_axis]['bow_step']:
                        homing_config['bow'] = self.axis[home_axis]['bow_step']

                # and do the homing
                self.machine.home(homing_config, timeout=self._homing_timeout)
                # better but still not good - we should have a better concept of 'axis'
                self.axis[home_axis]['homed'] = True
                self.axis_position[home_axis] = 0

    def set_position(self, positions):
        if positions:
            positions['type'] = 'set_position'
            # todo and what if there is no movement??
            self._print_queue.add_movement(positions)

    def relative_move_to(self, position):
        movement = {}
        for axis_name, pos in self.axis_position.iteritems():
            new_pos = pos
            if axis_name in position:
                new_pos += position[axis_name]
            movement[axis_name] = new_pos
        self.move_to(movement)

    # tuple with x/y/e coordinates - if left out no change is intended
    def move_to(self, position):
        if self.printing:
            position['type'] = 'move'
            self._print_queue.add_movement(position)
        else:
            self.start_print()
            position['type'] = 'move'
            self._print_queue.add_movement(position)
            self.finish_print()

    def execute_movement(self, movement):
        if movement['type'] == 'move':
            step_pos, step_speed_vector = self._add_movement_calculations(movement)
            x_move_config, y_move_config, z_move_config, e_move_config = self._generate_move_config(movement,
                                                                                                    step_pos,
                                                                                                    step_speed_vector)
            self._move(movement, step_pos, x_move_config, y_move_config, z_move_config, e_move_config)
        elif movement['type'] == 'set_position':
            for axis_name in self.axis:
                set_pos_name = "s%s" % axis_name
                if set_pos_name in movement:
                    position = movement[set_pos_name]
                    axis = self.axis[axis_name]
                    step_position = convert_mm_to_steps(position, axis['steps_per_mm'])
                    if 'motor' in axis and axis['motor']:
                        # todo one of the above should be enough
                        motor = axis['motor']
                        self.machine.set_pos(motor, step_position)
                    elif 'motors' in axis and axis['motors']:
                        for motor in axis['motors']:
                            self.machine.set_pos(motor, step_position)


    def set_fan(self, value):
        if value < 0:
            value = 0
        elif value > 1:
            value = 1
        PWM.set_duty_cycle(self._FAN_OUTPUT, value * 100.0)

    def run(self):
        self.led_manager.light(0, True)
        while self.running:
            if self.printing:
                try:
                    # get the next movement from stack
                    movement = self._print_queue.next_movement(self._print_queue_wait_time)
                    self.execute_movement(movement)
                except Empty:
                    _logger.debug("Print Queue did not return a value - this can be pretty normal")
            else:
                time.sleep(0.1)
        self.led_manager.light(0, False)

    def _configure_axis(self, axis, config):
        axis_name = axis['name']
        # let's see if we got one or more motors
        if 'motor' in config:
            axis['motor'] = config['motor']
        elif 'motors' in config:
            axis['motor'] = None
            axis['motors'] = config['motors']
        else:
            raise PrinterError("you must configure one ('motor') or more 'motors' in the axis configuration")

        axis['steps_per_mm'] = config['steps-per-mm']
        if 'step-scaling-correction' in config:
            _logger.debug("Scaling axis %s stepping %s by %s", axis_name, axis['steps_per_mm'],
                          config['step-scaling-correction'])
            step_scaling_correction = float(config['step-scaling-correction'])
            axis['steps_per_mm'] *= step_scaling_correction
        if 'time-reference' in config and config['time-reference'] == 'clock signal':
            axis['clock-referenced'] = True
        else:
            axis['clock-referenced'] = False

        axis['max_speed'] = config['max-speed']
        # todo - this can be clock signal referenced - convert acc. to  axis['clock-referenced']
        axis['max_speed_step'] = convert_mm_to_steps(config['max-speed'], config['steps-per-mm'])
        axis['max_acceleration'] = config['max-acceleration']
        axis['max_step_acceleration'] = convert_mm_to_steps(config['max-acceleration'], config['steps-per-mm'])
        if axis['max_step_acceleration'] > MAXIMUM_FREQUENCY_ACCELERATION:
            _logger.error("Acceleration of %s is higher than %s for axis %s!", axis['max_step_acceleration'],
                          MAXIMUM_FREQUENCY_ACCELERATION, axis_name)
            raise PrinterError("Acceleration for axis " + axis_name + " too high")
        if 'bow-acceleration' in config:
            axis['bow'] = config['bow-acceleration']
            axis['bow_step'] = convert_mm_to_steps(config['bow-acceleration'], config['steps-per-mm'])
            if axis['bow_step'] > MAXIMUM_FREQUENCY_BOW:
                _logger.error("Bow of %s is higher than %s for axis %s!", axis['bow_step'], MAXIMUM_FREQUENCY_BOW,
                              axis_name)
                raise PrinterError("Bow for axis " + axis_name + " too high")
        else:
            axis['bow'] = None
            axis['bow_step'] = None

        if 'home-speed' in config:
            axis['home_speed'] = config['home-speed']
        else:
            axis['home_speed'] = config['max-speed']
        if 'home-precision-speed' in config:
            axis['home_precision_speed'] = config['home-precision-speed']
        else:
            axis['home_precision_speed'] = config['max-speed'] / 10
        if 'home_acceleration' in config:
            axis['home_acceleration'] = config['home-acceleration']
            if convert_mm_to_steps(config['home-acceleration'],
                                   config['steps-per-mm']) > MAXIMUM_FREQUENCY_ACCELERATION:
                _logger.error("Homing acceleration of %s is higher than %s for axis %s!",
                              convert_mm_to_steps(config['home-acceleration'], config['steps-per-mm']),
                              MAXIMUM_FREQUENCY_ACCELERATION, axis_name)
            raise PrinterError("Acceleration for axis " + axis_name + " too high")
        else:
            axis['home_acceleration'] = config['max-acceleration']
        if 'home-retract' in config:
            axis['home_retract'] = config['home-retract']
        else:
            axis['home_retract'] = self._default_homing_retraction

        axis['end-stops'] = {}
        if 'end-stops' in config:
            _logger.debug("Configuring endstops for axis %s", axis_name)
            end_stops_config = config['end-stops']
            for end_stop_pos in ('left', 'right'):
                if end_stop_pos in end_stops_config:
                    _logger.debug("Configuring %s endstops", end_stop_pos)
                    end_stop_config = end_stops_config[end_stop_pos]
                    polarity = end_stop_config['polarity']
                    if 'virtual' == polarity:
                        position = float(end_stop_config['position'])
                        _logger.debug(" %s endstop is virtual at %s", end_stop_pos, position)
                        axis['end-stops'][end_stop_pos] = {
                            'type': 'virtual',
                            'position': position
                        }
                        # left endstop get's 0 posiotn - makes sense and distance for homing use
                        if end_stop_pos == 'left':
                            axis['end-stops'][end_stop_pos]['distance'] = axis['end-stops'][end_stop_pos]['position']
                            axis['end-stops'][end_stop_pos]['position'] = 0

                    elif polarity in ('positive', 'negative'):
                        _logger.debug(" %s endstop is real with %s polarity", end_stop_pos, polarity)
                        axis['end-stops'][end_stop_pos] = {
                            'type': 'real',
                            'polarity': polarity
                        }
                        if 'motor' in end_stop_config:
                            motor_ = end_stop_config['motor']
                            _logger.debug(" %s endstops applies to motor %s", end_stop_pos, motor_)
                            axis['end-stops'][end_stop_pos]['motor'] = motor_
                    else:
                        raise PrinterError("Unknown end stop type " + polarity)
                    end_stop = deepcopy(axis['end-stops'][end_stop_pos])
                    if 'position' in end_stop:
                        end_stop['position'] = convert_mm_to_steps(end_stop['position'], axis['steps_per_mm'])
                    if axis['motor']:
                        self.machine.configure_endstop(motor=axis['motor'], position=end_stop_pos,
                                                       end_stop_config=end_stop)
                    else:
                        # endstop config is a bit more complicated for multiple motors
                        if end_stop_config['polarity'] == 'virtual':
                            for motor in axis['motors']:
                                self.machine.configure_endstop(motor=motor, position=end_stop_pos,
                                                               end_stop_config=end_stop)
                        else:
                            if 'motor' in end_stop_config:
                                motor = end_stop_config['motor']
                            else:
                                motor = axis['motors'][0]
                            self.machine.configure_endstop(motor=motor, position=end_stop_pos, end_stop_config=end_stop)
        else:
            _logger.debug("No endstops for axis %s", axis_name)

        if 'encoder' in config:
            # read out the encoder config
            encoder_config = config['encoder']
            increments = int(encoder_config['increments-per-revolution'])
            if 'differential' in encoder_config and encoder_config['differential']:
                differential = True
            else:
                differential = False
            if 'inverted' in encoder_config and encoder_config['inverted']:
                inverted = True
            else:
                inverted = False
            axis['encoder'] = {
                'steps-per-rev': config['steps-per-revolution'],
                'increments-per-rev': increments,
                'differential': differential,
                'inverted': inverted
            }
            self.machine.configure_encoder(axis['motor'], deepcopy(axis['encoder']))

        current = config["current"]
        if axis["motor"]:
            self.machine.set_current(axis["motor"], current)
        else:
            for motor in axis['motors']:
                self.machine.set_current(motor, current)

        # let's see if there are any inverted motors
        if 'motor' in config:
            if "inverted" in config and config["inverted"]:
                axis['inverted'] = True
            else:
                axis['inverted'] = False
            self.machine.invert_motor(axis["motor"], axis['inverted'])
        else:
            # todo this is ok - but no perfect structure
            if "inverted" in config:
                axis['inverted'] = config["inverted"]
                for motor in axis['motors']:
                    if str(motor) in config['inverted'] and config['inverted'][str(motor)]:
                        self.machine.invert_motor(motor, True)

    def _configure_heater(self, heater_config):
        output_number = heater_config['output'] - 1
        if output_number < 0 or output_number >= len(beagle_bone_pins.pwm_config):
            raise PrinterError("PWM pins can only be between 1 and %s" % len(beagle_bone_pins.pwm_config))
        output = beagle_bone_pins.pwm_config[output_number]['out']
        thermometer = Thermometer(themistor_type=heater_config['sensor-type'],
                                  analog_input=beagle_bone_pins.pwm_config[output_number]['temp'])
        if 'current_input' in beagle_bone_pins.pwm_config[output_number]:
            current_pin = beagle_bone_pins.pwm_config[output_number]['current_input']
        else:
            current_pin = None
        type = heater_config['type']
        if type == 'PID':
            # do we have a maximum duty cycle??
            max_duty_cycle = None
            if 'max-duty-cycle' in heater_config:
                max_duty_cycle = heater_config['max-duty-cycle']
            pid_controller = PID(P=heater_config['pid-config']['Kp'],
                                 I=heater_config['pid-config']['Ki'],
                                 D=heater_config['pid-config']['Kd'],
                                 Integrator_max=heater_config['max-duty-cycle'])
            heater = PwmHeater(thermometer=thermometer, pid_controller=pid_controller,
                               output=output, maximum_duty_cycle=max_duty_cycle,
                               current_measurement=current_pin, machine=self.machine)
        elif type == "2 Point":
            hysteresis = heater_config['hysteresis']
            heater = OnOffHeater(thermometer=thermometer, output=output, active_high=True,
                                 hysteresis=hysteresis,
                                 current_measurement=current_pin, machine=self.machine)
        else:
            raise PrinterError("Unkown heater type %s" % type)
        return heater

    def _postconfig(self):
        # we need the stepping rations for variuos calclutaions later
        self._x_step_conversion = float(self.axis['x']['steps_per_mm']) / float(self.axis['y']['steps_per_mm'])
        self._y_step_conversion = float(self.axis['y']['steps_per_mm']) / float(self.axis['x']['steps_per_mm'])
        self._e_x_step_conversion = float(self.axis['e']['steps_per_mm']) / float(self.axis['x']['steps_per_mm'])
        self._e_y_step_conversion = float(self.axis['e']['steps_per_mm']) / float(self.axis['y']['steps_per_mm'])

        self._extract_homing_information()

        self.ready = True

    def _extract_homing_information(self):
        for axis_name, axis in self.axis.iteritems():
            axis['homeable'] = False
            if 'end-stops' in axis:
                for position in ['left', 'right']:
                    if position in axis['end-stops'] and not 'virtual' == axis['end-stops'][position]:
                        axis['homeable'] = True
                        axis['homed'] = False
                        break


    def _add_movement_calculations(self, movement):
        step_pos = {
            'x': convert_mm_to_steps(movement['x'], self.axis['x']['steps_per_mm']),
            'y': convert_mm_to_steps(movement['y'], self.axis['y']['steps_per_mm']),
            'z': convert_mm_to_steps(movement['z'], self.axis['z']['steps_per_mm']),
            'e': convert_mm_to_steps(movement['e'], self.axis['e']['steps_per_mm'])
        }
        relative_move_vector = movement['relative_move_vector']
        z_speed = min(abs(relative_move_vector['v'] * relative_move_vector['z']), self.axis['z']['max_speed'])
        e_speed = min(abs(relative_move_vector['v'] * relative_move_vector['e']), self.axis['e']['max_speed'])
        step_speed_vector = {
            # todo - this can be clock signal referenced - convert acc. to  axis['clock-referenced']
            'x': max(convert_mm_to_steps(abs(movement['speed']['x']), self.axis['x']['steps_per_mm']), 1),
            'y': max(convert_mm_to_steps(abs(movement['speed']['y']), self.axis['y']['steps_per_mm']), 1),
            'z': max(convert_mm_to_steps(z_speed, self.axis['z']['steps_per_mm']), 1),
            'e': max(convert_mm_to_steps(e_speed, self.axis['e']['steps_per_mm']), 1)
        }
        return step_pos, step_speed_vector

    def _generate_move_config(self, movement, step_pos, step_speed_vector):
        def _axis_movement_template(axis):
            return {
                'motor': axis['motor'],
                'acceleration': axis['max_step_acceleration'],
                'startBow': axis['bow_step'],
            }

        if movement['delta_x']:
            x_move_config = _axis_movement_template(self.axis['x'])
            x_move_config['target'] = step_pos['x']
            x_move_config['speed'] = abs(step_speed_vector['x'])
            if 'x_stop' in movement:
                x_move_config['type'] = 'stop'
            else:
                x_move_config['type'] = 'way'
        else:
            x_move_config = None

        if movement['delta_y']:
            y_move_config = _axis_movement_template(self.axis['y'])
            y_move_config['target'] = step_pos['y']
            y_move_config['speed'] = abs(step_speed_vector['y'])
            if 'y_stop' in movement:
                y_move_config['type'] = 'stop'
            else:
                y_move_config['type'] = 'way'
        else:
            y_move_config = None

        if movement['delta_z']:
            z_move_config = [
                {
                    'motor': self.axis['z']['motors'][0],
                    'target': step_pos['z'],
                    'acceleration': self.axis['z']['max_step_acceleration'],
                    'speed': abs(step_speed_vector['z']),
                    'type': 'stop',
                    'startBow': 0
                },
                {
                    'motor': self.axis['z']['motors'][1],
                    'target': step_pos['z'],
                    'acceleration': self.axis['z']['max_step_acceleration'],
                    'speed': abs(step_speed_vector['z']),
                    'type': 'stop',
                    'startBow': 0
                }
            ]
        else:
            z_move_config = None

        if movement['delta_e']:
            e_move_config = _axis_movement_template(self.axis['e'])
            e_move_config['target'] = step_pos['e']
            e_move_config['speed'] = abs(step_speed_vector['e'])
            if movement['e_stop']:
                e_move_config['type'] = 'stop'
            else:
                e_move_config['type'] = 'way'
        else:
            e_move_config = None

        return x_move_config, y_move_config, z_move_config, e_move_config

    def _move(self, movement, step_pos, x_move_config, y_move_config, z_move_config, e_move_config):
        move_vector = movement['relative_move_vector']
        move_commands = []
        if x_move_config and not y_move_config:  # silly, but simpler to understand
            # move x motor
            _logger.debug("Moving X axis to %s", step_pos['x'])

            move_commands = [
                x_move_config
            ]

        elif y_move_config and not x_move_config:  # still silly, but stil easier to understand
            # move y motor to position
            _logger.debug("Moving Y axis to %s", step_pos['y'])

            move_commands = [
                y_move_config
            ]
        elif x_move_config and y_move_config:
            # ok we have to see which axis has bigger movement
            if abs(movement['delta_x']) > abs(movement['delta_y']):
                y_factor = abs(move_vector['y'] / move_vector['x'] * self._y_step_conversion)
                _logger.debug(
                    "Moving X axis to %s gearing Y by %s to %s"
                    , step_pos['x'], y_factor, step_pos['y'])

                y_move_config['speed'] = x_move_config[
                                             'speed'] * y_factor
                y_move_config['acceleration'] = x_move_config[
                                                    'acceleration'] * y_factor  # todo or the max of the config/scaled??
                y_move_config['startBow'] = x_move_config['startBow'] * y_factor
                # move
                move_commands = [
                    x_move_config,
                    y_move_config
                ]
            else:
                x_factor = abs(move_vector['x'] / move_vector['y'] * self._x_step_conversion)
                _logger.debug(
                    "Moving Y axis to %s gearing X by %s  to %s"
                    , step_pos['x'], x_factor, step_pos['y'])

                x_move_config['speed'] = y_move_config[
                                             'speed'] * x_factor
                x_move_config['acceleration'] = y_move_config[
                                                    'acceleration'] * x_factor  # todo or the max of the config/scaled??
                x_move_config['startBow'] = y_move_config['startBow'] * x_factor
                # move
                move_commands = [
                    y_move_config,
                    x_move_config
                ]
        if e_move_config:
            if x_move_config and not (y_move_config and abs(move_vector['x']) < abs(move_vector['y'])):
                factor = abs(move_vector['e'] / move_vector['x'] * self._e_x_step_conversion)
                e_move_config['speed'] = factor * x_move_config['speed']
                e_move_config['acceleration'] = factor * x_move_config[
                    'acceleration']
                e_move_config['startBow'] = factor * x_move_config['startBow']
            elif y_move_config:
                factor = abs(move_vector['e'] / move_vector['y'] * self._e_y_step_conversion)
                e_move_config['speed'] = factor * y_move_config['speed']
                e_move_config['acceleration'] = factor * y_move_config[
                    'acceleration']
                e_move_config['startBow'] = factor * y_move_config['startBow']
            move_commands.append(e_move_config)

        if z_move_config:
            # todo we know that the z_move config is a list - is this too specific?
            move_commands.extend(z_move_config)

        # we update our position
        # todo isn't there a speedier way
        for axis_name in self.axis:
            self.axis_position[axis_name] = movement[axis_name]

        if move_commands:
            # we move only if there is something to move …
            self.machine.move_to(move_commands)


class PrintQueue():
    def __init__(self, axis_config, min_length, max_length, default_target_speed=None, led_manager=None):
        self.axis = axis_config
        self.planning_list = list()
        self.queue_size = min_length - 1  # since we got one extra
        self.queue = Queue(maxsize=(max_length - min_length))
        self.previous_movement = None
        # we will use the last_movement as special case since it may not fully configured
        self.default_target_speed = default_target_speed
        self.led_manager = led_manager

    def add_movement(self, target_position, timeout=None):
        # calculate the target
        move = self._extract_movement_values(target_position)
        # and see how fast we can allowable go
        # TODO currently the maximum achievable speed only considers x & y movements
        maximum_achievable_speed = self._maximum_achievable_speed(move)
        move['max_achievable_speed_vector'] = maximum_achievable_speed
        # and since we do not know it better the first guess is that the final speed is the max speed
        move['speed'] = maximum_achievable_speed
        # now we can push the previous move to the queue and recalculate the whole queue
        if self.previous_movement:
            self.planning_list.append(self.previous_movement)
            # if the list is long enough we can give it to the queue so that readers can get it
        if len(self.planning_list) > self.queue_size:
            self._push_from_planning_to_execution(timeout)
        self.previous_movement = move
        # and recalculate the maximum allowed speed
        self._recalculate_move_speeds()

    def next_movement(self, timeout=None):
        return self.queue.get(timeout=timeout)

    def finish(self, timeout=None):
        if self.previous_movement:
            self.previous_movement['x_stop'] = True
            self.previous_movement['y_stop'] = True
            self.previous_movement['e_stop'] = True
            self.planning_list.append(self.previous_movement)
            self.previous_movement = None
        while len(self.planning_list) > 0:
            self._push_from_planning_to_execution(timeout)
        while not self.queue.empty():
            pass

    def _push_from_planning_to_execution(self, timeout):
        executed_move = self.planning_list.pop(0)
        self.queue.put(executed_move, timeout=timeout)
        _logger.debug("adding to execution queue, now at %s/%s entries", len(self.planning_list), self.queue.qsize())

    def _extract_movement_values(self, target_position):
        move = {}
        if self.previous_movement:
            last_x = self.previous_movement['x']
            last_y = self.previous_movement['y']
            last_z = self.previous_movement['z']
            last_e = self.previous_movement['e']
        else:
            last_x = 0
            last_y = 0
            last_z = 0
            last_e = 0

        if target_position['type'] == 'move':
            move['type'] = 'move'
            # extract values
            # todo this can be for loop over axis_names
            if 'x' in target_position:
                move['x'] = target_position['x']
            else:
                if self.previous_movement:
                    move['x'] = self.previous_movement['x']
                else:
                    move['x'] = 0
            if 'y' in target_position:
                move['y'] = target_position['y']
            else:
                if self.previous_movement:
                    move['y'] = self.previous_movement['y']
                else:
                    move['y'] = 0
            if 'z' in target_position:
                move['z'] = target_position['z']
            else:
                if self.previous_movement:
                    move['z'] = self.previous_movement['z']
                else:
                    move['z'] = 0
            if 'e' in target_position:
                move['e'] = target_position['e']
            else:
                if self.previous_movement:
                    move['e'] = self.previous_movement['e']
                else:
                    move['e'] = 0
            if 'target_speed' in target_position:
                move['target_speed'] = target_position['target_speed']
            elif self.previous_movement:
                move['target_speed'] = self.previous_movement['target_speed']
            elif self.default_target_speed:
                move['target_speed'] = self.default_target_speed
            else:
                raise PrinterError("movement w/o a set speed and no default speed is set!")
            _logger.debug("moving to: X:%s, Y:%s, Z:%s", move['x'], move['z'], move['z'])

            move['delta_x'] = move['x'] - last_x
            move['delta_y'] = move['y'] - last_y
            move['delta_z'] = move['z'] - last_z
            move['delta_e'] = move['e'] - last_e
            move_vector = calculate_relative_vector(move['delta_x'], move['delta_y'], move['delta_z'], move['delta_e'])
            try:
                move_vector['v'] = move['target_speed'] / move_vector['l']
            except ZeroDivisionError:
                move_vector['v'] = 0
            # save the move vector for later use ...
            move['relative_move_vector'] = move_vector
        elif target_position['type'] == 'set_position':
            # a set position also means that we do not move it …
            move['x'] = last_x
            move['y'] = last_y
            move['z'] = last_z
            move['e'] = last_e
            move['delta_x'] = 0
            move['delta_y'] = 0
            move['delta_z'] = 0
            move['delta_e'] = 0
            move['relative_move_vector'] = {
                'x': 0.0,
                'z': 0.0,
                'y': 0.0,
                'e': 0.0,
                'l': 0.0,
            }
            move['target_speed'] = 0
            move['type'] = 'set_position'
            for axis, value in target_position.iteritems():
                if not axis == 'type':
                    move['s%s' % axis] = value
                    move[axis] = value

        return move

    def _maximum_achievable_speed(self, current_movement):
        if self.previous_movement and self.previous_movement['type'] == 'move':
            last_x_speed = self.previous_movement['speed']['x']
            last_y_speed = self.previous_movement['speed']['y']
        else:
            last_x_speed = 0
            last_y_speed = 0
        delta_x = current_movement['delta_x']
        delta_y = current_movement['delta_y']
        delta_e = current_movement['delta_e']
        normalized_move_vector = current_movement['relative_move_vector']
        # derive the various speed vectors from the movement … for desired head and maximum axis speed
        speed_vectors = [
            {
                # add the desired speed vector as initial value
                'x': current_movement['target_speed'] * normalized_move_vector['x'],
                'y': current_movement['target_speed'] * normalized_move_vector['y']
            }
        ]
        if delta_x != 0:
            scaled_y = normalized_move_vector['y'] / normalized_move_vector['x']
            speed_vectors.append({
                # what would the speed vector for max x speed look like
                'x': copysign(self.axis['x']['max_speed'], normalized_move_vector['x']),
                'y': copysign(self.axis['x']['max_speed'], normalized_move_vector['y']) * scaled_y
            })
            if not self.previous_movement or sign(delta_x) == sign(self.previous_movement['delta_x']):
                # ww can accelerate further
                start_velocity = last_x_speed
            else:
                # we HAVE to turn around!
                if self.previous_movement:
                    self.previous_movement['x_stop'] = True
                start_velocity = 0
            max_speed_x = get_target_velocity(start_velocity=start_velocity,
                                              length=delta_x,
                                              max_acceleration=self.axis['x']['max_acceleration'],
                                              jerk=self.axis['x']['bow'])
            speed_vectors.append({
                # how fast can we accelerate in X direction anyway
                'x': max_speed_x,
                'y': max_speed_x * scaled_y
            })
        else:
            # we HAVE to turn around!
            if self.previous_movement:
                self.previous_movement['x_stop'] = True

        if delta_y != 0:
            scaled_x = normalized_move_vector['x'] / normalized_move_vector['y']
            speed_vectors.append({
                # what would the maximum speed vector for y movement look like
                'x': copysign(self.axis['y']['max_speed'], normalized_move_vector['x']) * scaled_x,
                'y': copysign(self.axis['y']['max_speed'], normalized_move_vector['y'])
            })
            if not self.previous_movement or sign(delta_y) == sign(self.previous_movement['delta_y']):
                # ww can accelerate further
                start_velocity = last_y_speed
            else:
                # we HAVE to turn around!
                if self.previous_movement:
                    self.previous_movement['y_stop'] = True
                start_velocity = 0
            max_speed_y = get_target_velocity(start_velocity=start_velocity,
                                              length=delta_y,
                                              max_acceleration=self.axis['y']['max_acceleration'],
                                              jerk=self.axis['y']['bow'])
            speed_vectors.append({
                # how fast can we accelerate in X direction anyway
                'x': max_speed_y * scaled_x,
                'y': max_speed_y
            })
        else:
            # we HAVE to turn around!
            if self.previous_movement:
                self.previous_movement['y_stop'] = True
        if self.previous_movement:
            if delta_e != 0 and sign(delta_e) == sign(self.previous_movement['delta_e']):
                e_stop = ('x_stop' in self.previous_movement and self.previous_movement['x_stop']) \
                         and \
                         ('y_stop' in self.previous_movement and self.previous_movement['y_stop'])
                self.previous_movement['e_stop'] = e_stop
            else:
                self.previous_movement['e_stop'] = True

        max_local_speed_vector = find_shortest_vector(speed_vectors)
        # the minimum achievable speed is the minimum of all those local vectors

        return max_local_speed_vector


    def _recalculate_move_speeds(self):
        if self.led_manager:
            self.led_manager.light(2, True)

        x_bow_ = self.axis['x']['bow']
        y_bow_ = self.axis['y']['bow']
        x_max_acceleration = self.axis['x']['max_acceleration']
        y_max_acceleration = self.axis['y']['max_acceleration']

        next_move = self.previous_movement
        # we go back in the list and ensure that we can achieve the target speed with acceleration
        # and deceleration over the distance
        for current_move in reversed(self.planning_list):
            # if the next move is no move we ensure that we got a stop - hence most values are ignored
            if not next_move['type'] == 'move':
                current_move['x_stop'] = True
                current_move['y_stop'] = True
            next_target_speed = next_move['speed']
            # the movement we have calculated as achievable has to be considered anyway
            speed_vectors = [
                current_move['speed']
            ]
            current_move_vector = current_move['relative_move_vector']
            if current_move_vector['x'] != 0:
                if 'x_stop' in current_move and current_move['x_stop']:
                    # we must be able to stop in this move
                    start_velocity = 0.0
                    length = current_move['delta_x']
                elif 'x_stop' in next_move and next_move['x_stop']:
                    # we must be abel to stop in the next move
                    start_velocity = 0.0
                    length = next_move['delta_x']
                else:
                    # we have to achieve the target speed of the next move in the next move
                    start_velocity = next_target_speed['x']
                    length = next_move['delta_x']
                max_speed_x = get_target_velocity(start_velocity=start_velocity,
                                                  length=length,
                                                  max_acceleration=x_max_acceleration,
                                                  jerk=x_bow_)
                speed_vectors.append({
                    # what would the speed vector for max x speed look like
                    'x': max_speed_x,
                    'y': max_speed_x * current_move_vector['y'] / current_move_vector['x']
                })
            if current_move_vector['y'] != 0:
                if 'y_stop' in current_move and current_move['y_stop']:
                    # we must be able to stop in this move
                    start_velocity = 0.0
                    length = current_move['delta_y']
                elif 'y_stop' in next_move and next_move['y_stop']:
                    # we must be abel to stop in the next move
                    start_velocity = 0.0
                    length = next_move['delta_y']
                else:
                    # we have to achieve the target speed of the next move in the next move
                    start_velocity = next_target_speed['y']
                    length = next_move['delta_y']
                max_speed_y = get_target_velocity(start_velocity=start_velocity,
                                                  length=length,
                                                  max_acceleration=y_max_acceleration,
                                                  jerk=y_bow_)
                speed_vectors.append({
                    # what would the speed vector for max x speed look like
                    'x': max_speed_y * current_move_vector['x'] / current_move_vector['y'],
                    'y': max_speed_y
                })
            current_move['speed'] = find_shortest_vector(speed_vectors)
            # todo in theory we can stop if we did not change the speed vector ...
            next_move = current_move

        if self.led_manager:
            self.led_manager.light(2, False)


def get_target_velocity(start_velocity, length, max_acceleration, jerk):
    # the simple case is simple
    if not length or length == 0:
        return start_velocity
    # sanitize the values and convert to 'mathematics' form
    s = abs(float(length))
    v0 = abs(float(start_velocity))
    j = jerk
    # according to 'constant jerk equations for a trajectory generator'
    j_p2 = j * j
    ideal_s_curve_acceleration = calculate_ideal_s_curve_acceleration(j, v0, s)
    if ideal_s_curve_acceleration <= max_acceleration:
        # everything is fne we can go with a perfect s ramp
        velocity = v0 + ideal_s_curve_acceleration ** 2 / j
    else:
        # we have to include a constant acceleration phase
        a = max_acceleration
        a_p2 = a * a
        velocity = a_p2 / j + v0 - 1.0 / 2.0 * (3.0 * a_p2 + 2.0 * j * v0 - sqrt(
            a_p2 * a_p2 + 8.0 * a * j_p2 * s - 4.0 * a_p2 * j * v0 + 4.0 * j_p2 * v0 * v0)) / j
    return copysign(velocity / 2.0, start_velocity)  # todo this correction is neccessary - check fomula again


def calculate_ideal_s_curve_acceleration(j, v0, s):
    j_p2 = j * j
    sqrt_1_third = sqrt(1.0 / 3.0)
    term1 = pow((1.0 / 2.0 * j_p2 * s + 1.0 / 6.0 * sqrt_1_third *
                 sqrt((27.0 * j * s * s + 32.0 * v0 ** 3) * j) * j),
                (1.0 / 3.0))
    ideal_s_curve_acceleration = -2.0 / 3.0 * j * v0 / term1 + term1
    return ideal_s_curve_acceleration


# from http://www.physics.rutgers.edu/~masud/computing/WPark_recipes_in_python.html
def cbrt(x):
    from math import pow

    if x >= 0:
        return pow(x, 1.0 / 3.0)
    else:
        return -pow(abs(x), 1.0 / 3.0)


class PrinterError(Exception):
    def __init__(self, msg):
        self.msg = msg

