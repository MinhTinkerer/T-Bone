from Queue import Empty, Full
from hamcrest import assert_that, not_none, equal_to, close_to, less_than_or_equal_to, greater_than, less_than
from math import sqrt
import unittest
from trinamic_3d_printer.Printer import _calculate_relative_vector, find_shortest_vector, PrintQueue


class VectorTests(unittest.TestCase):
    def test_print_queue_blocking(self):
        default_timeout = 0.1
        axis_config = {
            'x': {
                'max_acceleration': 1,
                'max_speed': 1
            },
            'y': {
                'max_acceleration': 1,
                'max_speed': 1
            }
        }
        queue = PrintQueue(axis_config=axis_config, min_length=2, max_length=5)
        for i in range(5):
            position = {
                'x': i,
                'y': i,
                'f': 1
            }
            queue.add_movement(position, timeout=default_timeout)
        try:
            queue.add_movement({
                                   'x': 0,
                                   'y': 1
                               }, timeout=default_timeout)
            exception_thrown = False
        except Full:
            exception_thrown = True
        assert_that(exception_thrown, equal_to(True))
        for i in range(3):
            try:
                queue.next_movment(timeout=default_timeout)
                exception_thrown = False
            except Empty:
                exception_thrown = True
            assert_that(exception_thrown, equal_to(False))
        try:
            queue.next_movment(timeout=default_timeout)
            exception_thrown = False
        except Empty:
            exception_thrown = True
        assert_that(exception_thrown, equal_to(True))

    def test_print_queue_calculations(self):
        default_timeout = 0.1
        max_speed_x = 3
        max_speed_y = 2
        axis_config = {
            'x': {
                'max_acceleration': 0.5,
                'max_speed': max_speed_x
            },
            'y': {
                'max_acceleration': 0.5,
                'max_speed': max_speed_y
            }
        }
        queue = PrintQueue(axis_config=axis_config, min_length=20, max_length=21)
        #TODO add a movement to check if it accelerates correctly
        #we do not need any real buffer
        for i in range(6):
            queue.add_movement({
                'x': i + 1,
                'y': i + 1,
                'f': 10
            })
        last_movement = queue.last_movement
        assert_that(last_movement['speed'], not_none())
        assert_that(last_movement['speed']['x'], not_none())
        assert_that(last_movement['speed']['x'], less_than_or_equal_to(max_speed_x))
        assert_that(last_movement['speed']['y'], not_none())
        assert_that(last_movement['speed']['y'], less_than_or_equal_to(max_speed_y))
        queue.add_movement({
            'x': 7,
            'y': 6
        })
        last_movement = queue.last_movement
        assert_that(last_movement['speed'], not_none())
        assert_that(last_movement['speed']['x'], not_none())
        assert_that(last_movement['speed']['x'], less_than_or_equal_to(max_speed_x))
        assert_that(last_movement['speed']['x'], greater_than(0))
        assert_that(last_movement['speed']['y'], not_none())
        assert_that(last_movement['speed']['y'], equal_to(0))
        previous_movement = queue.planning_list[-1]
        assert_that(previous_movement['speed']['x'], not_none())
        assert_that(previous_movement['speed']['x'], less_than(max_speed_x))
        assert_that(previous_movement['speed']['y'], less_than(max_speed_y))
        assert_that(previous_movement['speed']['y'], greater_than(0))
        previous_movement = queue.planning_list[-3]
        assert_that(previous_movement['speed']['x'], not_none())
        assert_that(previous_movement['speed']['x'], equal_to(max_speed_x))
        assert_that(previous_movement['speed']['y'], equal_to(max_speed_y))
        queue.add_movement({
            'x': 5,
            'y': 6
        })
        last_movement = queue.last_movement
        #boring test but brings a break point
        assert_that(last_movement['speed']['x'], less_than_or_equal_to(max_speed_x))


    def test_vector_math(self):
        result = _calculate_relative_vector(1, 1)
        assert_that(result, not_none())
        assert_that(result['x'], close_to(1 / sqrt(2), 0.0001))
        assert_that(result['y'], close_to(1 / sqrt(2), 0.0001))
        assert_that(result['l'], close_to(1, 0.0001))

        result = _calculate_relative_vector(23, 23)
        assert_that(result, not_none())
        assert_that(result['x'], close_to(1 / sqrt(2), 0.0001))
        assert_that(result['y'], close_to(1 / sqrt(2), 0.0001))
        assert_that(result['l'], close_to(1, 0.0001))

        result = _calculate_relative_vector(0, 0)
        assert_that(result, not_none())
        assert_that(result['x'], equal_to(0))
        assert_that(result['y'], equal_to(0))
        assert_that(result['l'], equal_to(0))

        result = _calculate_relative_vector(0, 20)
        assert_that(result, not_none())
        assert_that(result['x'], equal_to(0))
        assert_that(result['y'], equal_to(1))
        assert_that(result['l'], equal_to(1))

    def test_vector_comparison(self):
        testvectors = [
            {
                'x': 1,
                'y': 1
            },
            {
                'x': 0.5,
                'y': 0.5
            },
            {
                'x': 2.3,
                'y': 2.3
            }
        ]
        result = find_shortest_vector(testvectors)
        assert_that(result['x'], equal_to(0.5))