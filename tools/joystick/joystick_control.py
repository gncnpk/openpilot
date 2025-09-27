#!/usr/bin/env python3
import os
import argparse
import threading
import time
import signal
import sys
import numpy as np
from inputs import UnpluggedError, get_gamepad

from cereal import messaging
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.system.hardware import HARDWARE
from openpilot.tools.lib.kbhit import KBHit

EXPO = 0.4

# Global publisher instance for cleanup
global_publisher = None
shutdown_flag = threading.Event()


def signal_handler(signum, frame):
  """Handle shutdown signals gracefully"""
  global global_publisher
  print("\nShutdown signal received. Cleaning up...")
  shutdown_flag.set()
  if global_publisher:
    try:
      del global_publisher
    except Exception:
      pass
  sys.exit(0)


class Keyboard:
  def __init__(self):
    self.kb = KBHit()
    self.axis_increment = 0.05  # 5% of full actuation each key press
    self.axes_map = {'w': 'gb', 's': 'gb',
                     'a': 'steer', 'd': 'steer'}
    self.axes_values = {'gb': 0., 'steer': 0.}
    self.axes_order = ['gb', 'steer']
    self.cancel = False

  def update(self):
    key = self.kb.getch().lower()
    self.cancel = False
    if key == 'r':
      self.axes_values = dict.fromkeys(self.axes_values, 0.)
    elif key == 'c':
      self.cancel = True
    elif key in self.axes_map:
      axis = self.axes_map[key]
      incr = self.axis_increment if key in ['w', 'a'] else -self.axis_increment
      self.axes_values[axis] = float(np.clip(self.axes_values[axis] + incr, -1, 1))
    else:
      return False
    return True


class Joystick:
  def __init__(self):
    # This class supports a PlayStation 5 DualSense controller on the comma 3X
    # TODO: find a way to get this from API or detect gamepad/PC, perhaps "inputs" doesn't support it
    self.cancel_button = 'BTN_NORTH'  # BTN_NORTH=X/triangle
    if HARDWARE.get_device_type() == 'pc':
      accel_axis = 'ABS_Z'
      steer_axis = 'ABS_RX'
      # TODO: once the longcontrol API is finalized, we can replace this with outputting gas/brake and steering
      self.flip_map = {'ABS_RZ': accel_axis}
    else:
      accel_axis = 'ABS_RX'
      steer_axis = 'ABS_Z'
      self.flip_map = {'ABS_RY': accel_axis}

    self.min_axis_value = {accel_axis: 0., steer_axis: 0.}
    self.max_axis_value = {accel_axis: 255., steer_axis: 255.}
    self.axes_values = {accel_axis: 0., steer_axis: 0.}
    self.axes_order = [accel_axis, steer_axis]
    self.cancel = False

  def update(self):
    try:
      joystick_event = get_gamepad()[0]
    except (OSError, UnpluggedError):
      self.axes_values = dict.fromkeys(self.axes_values, 0.)
      return False

    event = (joystick_event.code, joystick_event.state)

    # flip left trigger to negative accel
    if event[0] in self.flip_map:
      event = (self.flip_map[event[0]], -event[1])

    if event[0] == self.cancel_button:
      if event[1] == 1:
        self.cancel = True
      elif event[1] == 0:   # state 0 is falling edge
        self.cancel = False
    elif event[0] in self.axes_values:
      self.max_axis_value[event[0]] = max(event[1], self.max_axis_value[event[0]])
      self.min_axis_value[event[0]] = min(event[1], self.min_axis_value[event[0]])

      norm = -float(np.interp(event[1], [self.min_axis_value[event[0]], self.max_axis_value[event[0]]], [-1., 1.]))
      norm = norm if abs(norm) > 0.03 else 0.  # center can be noisy, deadzone of 3%
      self.axes_values[event[0]] = EXPO * norm ** 3 + (1 - EXPO) * norm  # less action near center for fine control
    else:
      return False
    return True


def send_thread(joystick):
  global global_publisher
  pm = None
  max_retries = 5
  retry_delay = 1.0

  for attempt in range(max_retries):
    if shutdown_flag.is_set():
      return
    try:
      pm = messaging.PubMaster(['testJoystick'])
      global_publisher = pm
      break
    except Exception as e:
      if "Address already in use" in str(e) or "MultiplePublishersError" in str(e):
        print(f"Publisher conflict detected (attempt {attempt + 1}/{max_retries}). Waiting {retry_delay}s before retry...")
        time.sleep(retry_delay)
        retry_delay *= 1.5  # Exponential backoff
      else:
        raise e

  if pm is None:
    print("Failed to create publisher after all retries. Exiting.")
    return

  rk = Ratekeeper(100, print_delay_threshold=None)

  while not shutdown_flag.is_set():
    try:
      if rk.frame % 20 == 0:
        print('\n' + ', '.join(f'{name}: {round(v, 3)}' for name, v in joystick.axes_values.items()))

      joystick_msg = messaging.new_message('testJoystick')
      joystick_msg.valid = True
      joystick_msg.testJoystick.axes = [joystick.axes_values[ax] for ax in joystick.axes_order]

      pm.send('testJoystick', joystick_msg)

      rk.keep_time()
    except Exception as e:
      if "MultiplePublishersError" in str(e) or "Address already in use" in str(e):
        print("Publisher conflict during runtime. Attempting to restart publisher...")
        # Try to recreate the publisher
        try:
          pm = messaging.PubMaster(['testJoystick'])
          global_publisher = pm
        except Exception:
          print("Failed to recreate publisher. Exiting thread.")
          break
      else:
        print(f"Unexpected error in send_thread: {e}")
        break


def cleanup_existing_publishers():
  """Attempt to clean up any existing testJoystick publishers"""
  try:
    # Try to create a temporary publisher to trigger cleanup of existing ones
    temp_pm = messaging.PubMaster(['testJoystick'])
    time.sleep(0.1)  # Brief delay to allow cleanup
    del temp_pm
  except Exception as e:
    if "Address already in use" in str(e) or "MultiplePublishersError" in str(e):
      print("Detected existing publisher, waiting for cleanup...")
      time.sleep(2.0)
    else:
      print(f"Unexpected error during cleanup: {e}")


def joystick_control_thread(joystick):
  Params().put_bool('JoystickDebugMode', True)

  # Attempt to cleanup any existing publishers
  cleanup_existing_publishers()

  sender_thread = threading.Thread(target=send_thread, args=(joystick,), daemon=True)
  sender_thread.start()

  try:
    while not shutdown_flag.is_set():
      if not joystick.update():
        time.sleep(0.01)  # Small delay if no joystick input
  except KeyboardInterrupt:
    print("\nJoystick control interrupted")
  finally:
    shutdown_flag.set()
    sender_thread.join(timeout=1.0)


def main():
  joystick_control_thread(Joystick())


if __name__ == '__main__':
  # Register signal handlers for graceful shutdown
  signal.signal(signal.SIGINT, signal_handler)
  signal.signal(signal.SIGTERM, signal_handler)

  parser = argparse.ArgumentParser(description='Publishes events from your joystick to control your car.\n' +
                                               'openpilot must be offroad before starting joystick_control. This tool supports ' +
                                               'a PlayStation 5 DualSense controller on the comma 3X.',
                                   formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument('--keyboard', action='store_true', help='Use your keyboard instead of a joystick')
  args = parser.parse_args()

  if not Params().get_bool("IsOffroad") and "ZMQ" not in os.environ:
    print("The car must be off before running joystick_control.")
    exit()

  print()
  if args.keyboard:
    print('Gas/brake control: `W` and `S` keys')
    print('Steering control: `A` and `D` keys')
    print('Buttons')
    print('- `R`: Resets axes')
    print('- `C`: Cancel cruise control')
  else:
    print('Using joystick, make sure to run cereal/messaging/bridge on your device if running over the network!')
    print('If not running on a comma device, the mapping may need to be adjusted.')

  joystick = Keyboard() if args.keyboard else Joystick()

  try:
    joystick_control_thread(joystick)
  except KeyboardInterrupt:
    print("\nKeyboard interrupt received. Shutting down...")
  finally:
    signal_handler(None, None)
