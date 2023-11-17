#!/bin/bash

ansible all -m apt -a "upgrade=dist" --become --ask-become-pass
