#!/bin/bash

#Author: Alex
#Purpose: Run dis-upgrade against my Ubuntu VMs 

ansible all -m apt -a "upgrade=dist" --become --ask-become-pass
