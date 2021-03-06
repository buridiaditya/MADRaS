"""
Gym Madras Env Wrapper.

This is an OpenAI gym environment wrapper for the MADRaS simulator. For more information on the OpenAI Gym interface, please refer to: https://gym.openai.com

Built on top of gym_torcs https://github.com/ugo-nama-kun/gym_torcs/blob/master/gym_torcs.py

The following enhancements were made for Multi-agent synchronization using exception handling:
- All the agents connect to the same TORCS engine through UDP ports
- If an agent fails to connect to the TORCS engine, it keeps on trying in a loop until successful
- Restart the episode for all the agents when any one of the learning agents terminates its episode

"""

import math
from copy import deepcopy
import numpy as np
import MADRaS.utils.snakeoil3_gym as snakeoil3
from MADRaS.utils.gym_torcs import TorcsEnv
import MADRaS.traffic.const_vel as agentConstant
import MADRaS.traffic.rand_stop as agentStopper
import MADRaS.traffic.vel_change as agentSinusoid
import MADRaS.traffic.lane_switch as agentLaneChanger

from MADRaS.controllers.pid import PID
import gym
from gym.utils import seeding
import os
import subprocess
import signal
import time
from mpi4py import MPI
import random
import socket

from multiprocessing import Process

class MadrasEnv(TorcsEnv,gym.Env):
    """Definition of the Gym Madras Env."""
    def __init__(self, vision=False, throttle=True,
                 gear_change=False, port=60934, pid_assist=False,
                 CLIENT_MAX_STEPS=np.inf,visualise=True,no_of_visualisations=1, multi_agent_mode=False ,random_traffic=True, traffic_type=[]):
        # traffic_type is a list of traffic agents.
        # If `visualise` is set to False torcs simulator will run in headless mode
        """Init Method."""
        self.torcs_proc = None
        self.pid_assist = pid_assist
        self.traffic_type = traffic_type
        if random_traffic:
            self.traffic_type = np.random.randint(0,4,len(traffic_type))
        if self.pid_assist:
            self.action_dim = 2  # LanePos, Velocity
        else:
            self.action_dim = 3  # Accel, Steer, Brake
        TorcsEnv.__init__(self, vision=False, throttle=True, gear_change=False,visualise=visualise,no_of_visualisations=no_of_visualisations)
        self.state_dim = 29  # No. of sensors input
        self.env_name = 'Madras_Env'
        self.port = port
        self.visualise = visualise
        self.no_of_visualisations = no_of_visualisations
        self.CLIENT_MAX_STEPS = CLIENT_MAX_STEPS
        self.client_type = 0  # Snakeoil client type
        self.initial_reset = True
        self.early_stop = True
        if self.pid_assist:
            self.PID_latency = 10
        else:
            self.PID_latency = 1
        self.accel_PID = PID(np.array([10.5, 0.05, 2.8]))  # accel PID
        self.steer_PID = PID(np.array([5.1, 0.001, 0.000001]))  # steer PID

        self.prev_lane = 0
        self.prev_angle = 0
        self.prev_vel = 0
        self.prev_dist = 0
        self.ob = None
        self.track_len = 7014.6
        self.seed()
        if multi_agent_mode == False:
            self.start_torcs_process()

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

        
    def get_free_udp_port(self):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(('', 0))
        addr, port = udp.getsockname()
        udp.close()
        return port

    def start_torcs_process(self):
        if self.torcs_proc is not None:
            os.killpg(os.getpgid(self.torcs_proc.pid), signal.SIGKILL)
            time.sleep(0.5)
            self.torcs_proc = None

        self.port = self.get_free_udp_port()
        window_title = str(self.port)
        command = None
        rank = MPI.COMM_WORLD.Get_rank()

        
        if rank < self.no_of_visualisations and self.visualise:
            command = 'export TORCS_PORT={} && vglrun torcs -t 10000000 -nolaptime'.format(self.port)
        else:
            command = 'export TORCS_PORT={} && torcs -t 10000000 -r ~/.torcs/config/raceman/quickrace.xml -nolaptime'.format(self.port)
        if self.vision is True:
            command += ' -vision'

        self.torcs_proc = subprocess.Popen([command], shell=True, preexec_fn=os.setsid)
        time.sleep(1)
        #if self.visualise:
        #    os.system('sh autostart.sh {}'.format(window_title))

   
    def reset(self, prev_step_info=None):
        """Reset Method. To be called at the end of each episode"""
        if self.initial_reset:
           
            self.traffic_processes = []
            
            self.ports = [self.port+p for p in range(1+len(self.traffic_type))]
            
            index = 0
            if len(self.traffic_type) != 0:                
                index = random.randint(1,len(self.ports)-1)
            self.oldpos = index+1

            self.mainport = self.ports.pop(index)

            for i in range(len(self.traffic_type)):
                tp = self.traffic_type[i]
                if tp == 0: # constant velocity traffic
                    p1 = Process(target = agentConstant.playTraffic, args=(self.ports[i],) )
                    self.traffic_processes.append(p1)
                elif tp == 1: # random stopping traffic
                    p1 = Process(target = agentStopper.playTraffic, args=(self.ports[i],) )
                    self.traffic_processes.append(p1)
                elif tp == 2: # Sinusoidal traffic
                    p1 = Process(target = agentSinusoid.playTraffic, args=(self.ports[i],) )
                    self.traffic_processes.append(p1)
                elif tp == 3: # Lane changing
                    p1 = Process(target = agentLaneChanger.playTraffic, args=(self.ports[i],) )
                    self.traffic_processes.append(p1)

            for p in self.traffic_processes:
                p.start()
            
            while self.ob is None:
                try:
                    self.client = snakeoil3.Client(p=self.mainport,
                                                   vision=self.vision,visualise=self.visualise)
                    # Open new UDP in vtorcs
                    self.client.MAX_STEPS = self.CLIENT_MAX_STEPS
                    self.client.get_servers_input(step=0)
                    # Get the initial input from torcs
                    raw_ob = self.client.S.d
                    # Get the current full-observation
                    self.ob = self.make_observation(raw_ob)
                except:
                    pass
            self.initial_reset = False

        else:
            try:

                for p in self.traffic_processes:
                    p.terminate()

                self.traffic_processes = []                                
                self.ports = [self.port+p for p in range(1+len(self.traffic_type))]
                index = 0
                if len(self.traffic_type) != 0:                
                    index = random.randint(1,len(self.ports)-1)
                self.oldpos = index+1                
                self.mainport = self.ports.pop(index)


                for i in range(len(self.traffic_type)):
                    tp = self.traffic_type[i]
                    if tp == 0: # constant velocity traffic
                        p1 = Process(target = agentConstant.playTraffic, args=(self.ports[i],) )
                        self.traffic_processes.append(p1)
                    elif tp == 1: # random stopping traffic
                        p1 = Process(target = agentStopper.playTraffic, args=(self.ports[i],) )
                        self.traffic_processes.append(p1)
                    elif tp == 2: # Sinusoidal traffic
                        p1 = Process(target = agentSinusoid.playTraffic, args=(self.ports[i],) )
                        self.traffic_processes.append(p1)
                    elif tp == 3: # random stopping traffic
                        p1 = Process(target = agentLaneChanger.playTraffic, args=(self.ports[i],) )
                        self.traffic_processes.append(p1)

                for p in self.traffic_processes:
                    p.start() 
                
                self.ob, self.client = TorcsEnv.reset(self, client=self.client, serverport=self.port, agentport=self.mainport, relaunch=True)
                
                    
            except Exception as e:
                self.ob = None
                while self.ob is None:
                    try:
                        print("Hard Reset")
                        # self.end(self.client)
                        self.client = snakeoil3.Client(p=self.mainport,
                                                       vision=self.vision)
                        # Open new UDP in vtorcs
                        self.client.MAX_STEPS = self.CLIENT_MAX_STEPS
                        self.client.get_servers_input(step=0)
                        # Get the initial input from torcs
                        raw_ob = self.client.S.d
                        # Get the current full-observation from torcs
                        self.ob = self.make_observation(raw_ob)
                    except:
                        pass

        self.distance_traversed = 0
        s_t = np.hstack((self.ob.angle, self.ob.track, self.ob.trackPos,
                        self.ob.speedX, self.ob.speedY, self.ob.speedZ,
                        self.ob.wheelSpinVel / 100.0, self.ob.rpm))

        return s_t

    def step(self, desire):
        """Step method to be called at each time step."""
        r_t = 0

        for PID_step in range(self.PID_latency):
                # Implement the desired trackpos and velocity using PID
            if self.pid_assist:
                self.accel_PID.update_error((desire[1] - self.prev_vel))
                self.steer_PID.update_error((-(self.prev_lane - desire[0]) / 10 +
                                            self.prev_angle))
                if self.accel_PID.output() < 0.0:
                    brake = 1
                else:
                    brake = 0
                a_t = np.asarray([self.steer_PID.output(),
                                 self.accel_PID.output(), brake])
            else:
                a_t = desire
            try:
                self.ob, r, done, info = TorcsEnv.step(self, PID_step,
                                                       self.client, a_t,
                                                       self.early_stop)
            except Exception as e:
                print(("Exception caught at port " + str(e)))
                self.ob = None
                while self.ob is None:
                    try:
                        self.client = snakeoil3.Client(p=self.mainport,
                                                       vision=self.vision)
                        # Open new UDP in vtorcs
                        self.client.MAX_STEPS = self.CLIENT_MAX_STEPS
                        self.client.get_servers_input(0)
                        # Get the initial input from torcs
                        raw_ob = self.client.S.d
                        # Get the current full-observation from torcs
                        self.ob = self.make_observation(raw_ob)
                    except:
                        pass
                    continue
            self.prev_vel = self.ob.speedX
            self.prev_angle = self.ob.angle
            self.prev_lane = self.ob.trackPos
            if (math.isnan(r)):
                r = 0.0
            r_t += r  # accumulate rewards over all the time steps

            self.distance_traversed = self.client.S.d['distRaced']
            if self.distance_traversed - self.prev_dist == 0:
                r_t -= 50
            # r_t += (self.distance_traversed - self.prev_dist) /\
                # self.track_len
            # r_t = self.distance_traversed
            # r_t += (4-self.client.S.d['racePos'])
            self.prev_dist = deepcopy(self.distance_traversed)
            
            if self.client.S.d['racePos'] < self.oldpos:
                print("Overtake done")
                r_t += 10000

            if self.client.S.d['racePos'] > self.oldpos:
                print("Overtakeen by traffic")
                r_t -= 10000

            if self.client.S.d['racePos'] == 1 and (len(self.traffic_type) != 0):
                print("Reached Position 1 - Resetting")
                done = True
                r_t += 50000


            self.oldpos = self.client.S.d['racePos']

            if self.distance_traversed >= self.track_len:
                # reward += 1000
                done = True
            if done:
                # self.reset()
                break

        s_t1 = np.hstack((self.ob.angle, self.ob.track, self.ob.trackPos,
                          self.ob.speedX, self.ob.speedY, self.ob.speedZ,
                          self.ob.wheelSpinVel / 100.0, self.ob.rpm))

        return s_t1, r_t, done, info
