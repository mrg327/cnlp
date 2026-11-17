cmake_minimum_required(VERSION 3.16)
# Generated board component lands in components/board (run `cnlp gen`).
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(blinky)
