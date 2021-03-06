#!/usr/bin/python

# Implement PD on real vehicle
import rospy
from std_msgs.msg import Header
from std_msgs.msg import Int32, Bool
from std_msgs.msg import String
from sensor_msgs.msg import Joy
from sensor_msgs.msg import Imu
from geometry_msgs.msg import PoseStamped, TwistStamped

import os
import time
import numpy as np
import math
from LLC import pid
from matplotlib import pyplot as plt

from src.EpisodeManager import *

def quatToEuler(quat):
    x = quat[0]
    y = quat[1]
    z = quat[2]
    w = quat[3]

    t0 = +2.0 * (w * x + y * z)
    t1 = +1.0 - 2.0 * (x * x + y * y)
    X = math.degrees(math.atan2(t0, t1))

    t2 = +2.0 * (w * y - z * x)
    t2 = +1.0 if t2 > +1.0 else t2
    t2 = -1.0 if t2 < -1.0 else t2
    Y = math.degrees(math.asin(t2))

    t3 = +2.0 * (w * z + x * y)
    t4 = +1.0 - 2.0 * (y * y + z * z)
    Z = math.degrees(math.atan2(t3, t4))

    return X, Y, Z

class LLCEnv:

    # CALLBACKS
    def VehiclePositionCB(self,stamped_pose):
        # new_stamped_pose = urw.positionROS2RW(stamped_pose)
        x = stamped_pose.pose.position.x
        y = stamped_pose.pose.position.y
        z = stamped_pose.pose.position.z
        self.world_state['VehiclePos'] = np.array([x,y,z])

        qx = stamped_pose.pose.orientation.x
        qy = stamped_pose.pose.orientation.y
        qz = stamped_pose.pose.orientation.z
        qw = stamped_pose.pose.orientation.w
        self.world_state['VehicleOrien'] = np.array([qx,qy,qz,qw])

    def VehicleVelocityCB(self, stamped_twist):
        vx = stamped_twist.twist.linear.x
        vy = stamped_twist.twist.linear.y
        vz = stamped_twist.twist.linear.z
        self.world_state['VehicleLinearVel'] = np.array([vx,vy,vz])

        wx = stamped_twist.twist.angular.x
        wy = stamped_twist.twist.angular.y
        wz = stamped_twist.twist.angular.z
        self.world_state['VehicleAngularVel'] = np.array([wx,wy,wz])

    def ArmHeightCB(self, data):
        height = data.data
        self.world_state['ArmHeight'] = np.array([height])

    def BladeImuCB(self, imu):
        qx = imu.orientation.x
        qy = imu.orientation.y
        qz = imu.orientation.z
        qw = imu.orientation.w
        self.world_state['BladeOrien'] = np.array([qx,qy,qz,qw])

        wx = imu.angular_velocity.x
        wy = imu.angular_velocity.y
        wz = imu.angular_velocity.z
        self.world_state['BladeAngularVel'] = np.array([wx,wy,wz])

        ax = imu.linear_acceleration.x
        ay = imu.linear_acceleration.y
        az = imu.linear_acceleration.z
        self.world_state['BladeLinearAcc'] = np.array([ax,ay,az])

    def do_action(self, pd_action):
        joymessage = Joy()

        joyactions = self.PDToJoyAction(pd_action)  # clip actions to fit action_size

        joymessage.axes = [joyactions[0], 0., joyactions[2], joyactions[3], joyactions[4], joyactions[5], 0., 0.]

        self.joypub.publish(joymessage)
        rospy.logdebug(joymessage)

    def PDToJoyAction(self, pd_action):
        # translate chosen action (array) to joystick action (dict)

        joyactions = np.zeros(6)
        joyactions[2] = 1. # default value
        joyactions[5] = 1. # default value

        # ONLY LIFT AND PITCH
        joyactions[3] = pd_action[1] # blade pitch
        joyactions[4] = pd_action[0] # blade lift

        return joyactions


    def __init__(self, L):
        self._output_folder = os.getcwd()

        self.world_state = {}
        self.simOn = False
        self.keys = ['ArmHeight', 'BladeOrien']
        self.length = L

        # For time step
        self.current_time = time.time()
        self.last_time = self.current_time
        self.time_step = []
        self.last_obs = np.array([])
        self.TIME_STEP = 0.05

        ## ROS messages
        rospy.init_node('slagent', anonymous=False)
        self.rate = rospy.Rate(10)  # 10hz

        # Define Subscribers
        # self.vehiclePositionSub = rospy.Subscriber('mavros/local_position/pose', PoseStamped, self.VehiclePositionCB)
        # self.vehicleVelocitySub = rospy.Subscriber('mavros/local_position/velocity', TwistStamped, self.VehicleVelocityCB)
        self.heightSub = rospy.Subscriber('arm/height', Int32, self.ArmHeightCB)
        self.bladeImuSub = rospy.Subscriber('arm/blade/Imu', Imu, self.BladeImuCB)

        # Define Publisher
        self.joypub = rospy.Publisher('joy', Joy, queue_size=10)

        # initiate simulation
        self.init_env()
        time.sleep(3)

        # Define PIDs
        # set point = height of 100, pitch of 0
        self._kp_kd = 'kp=0.1_kd=0.01'
        self.lift_pid = pid.PID(P=0.1, I=0, D=0.01, saturation=True)
        self.lift_pid.SetPoint = 100.0
        self.lift_pid.setSampleTime(0.01)

        self.pitch_pid = pid.PID(P=0.1, I=0, D=0.01, saturation=True) # P=10.0, I=0, D=0.001
        self.pitch_pid.SetPoint = 0.
        self.pitch_pid.setSampleTime(0.01)

        # init plot
        x = np.linspace(0, self.length, self.length + 1)
        self.fig, (self.ax_lift, self.ax_pitch) = plt.subplots(2)
        self.ax_lift.plot(x, np.array(x.size * [self.lift_pid.SetPoint]))
        self.ax_lift.set_title('lift')
        self.ax_pitch.plot(x, np.array(x.size *[self.pitch_pid.SetPoint]))
        self.ax_pitch.set_title('pitch')
        self.fig.show()


    def init_env(self):
        if self.simOn:
            self.episode.killSimulation()

        self.episode = EpisodeManager()
        self.episode.generateAndRunWholeEpisode(typeOfRand="verybasic")  # for NUM_STONES = 1
        self.simOn = True


    def step(self, i):
        stop = False

        while True: # wait for all topics to arrive
            if all(key in self.world_state for key in self.keys):
                break

        # for even time steps
        self.current_time = time.time()
        time_step = self.current_time - self.last_time

        if time_step < self.TIME_STEP:
            time.sleep(self.TIME_STEP - time_step)
            self.current_time = time.time()
            time_step = self.current_time - self.last_time

        self.time_step.append(time_step)
        self.last_time = self.current_time

        # current state
        current_lift = self.world_state['ArmHeight'].item(0)
        current_pitch = quatToEuler(self.world_state['BladeOrien'])[1]
        # print(quatToEuler(self.world_state['BladeOrien']))
        print('lift = ', current_lift, 'pitch = ', current_pitch)

        # check if done
        if current_lift == self.lift_pid.SetPoint and current_pitch == self.pitch_pid.SetPoint:
            print('Success!')
            stop = True

        # pid update
        lift_output = self.lift_pid.update(current_lift)
        pitch_output = self.pitch_pid.update(current_pitch)

        # do action in simulation
        pd_action = np.array([lift_output, pitch_output])
        self.do_action(pd_action)

        # plot
        self.ax_lift.scatter(i, current_lift, color='red')
        self.ax_pitch.scatter(i, current_pitch, color='red')
        self.fig.show()

        return stop

    def save_plot(self):
        # create plot folder if it does not exist
        try:
            plot_folder = "{}/plots".format(self._output_folder)
        except FileNotFoundError:
            os.makedirs(plot_folder)
        self.fig.savefig('{}/{}.png'.format(plot_folder, self._kp_kd))
        print('figure saved!')


if __name__ == '__main__':
    L = 100
    LLC = LLCEnv(L)
    for i in range(L):
        stop = LLC.step(i)
        if i == L-1:
            LLC.save_plot()
        if stop:
            break

