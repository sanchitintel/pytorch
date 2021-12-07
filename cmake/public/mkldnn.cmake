set(MKLDNN_USE_NATIVE_ARCH ${USE_NATIVE_ARCH})

if(CPU_AARCH64)
  include(${CMAKE_CURRENT_LIST_DIR}/ComputeLibrary.cmake)
endif()

find_package(MKLDNN QUIET)

if(NOT TARGET caffe2::mkldnn)
  add_library(caffe2::mkldnn INTERFACE IMPORTED)
endif()

set_property(
  TARGET caffe2::mkldnn PROPERTY INTERFACE_INCLUDE_DIRECTORIES
  ${MKLDNN_INCLUDE_DIR})
set_property(
  TARGET caffe2::mkldnn PROPERTY INTERFACE_LINK_LIBRARIES
  ${MKLDNN_LIBRARIES})
if(NOT APPLE AND NOT WIN32)
  if(NOT TARGET caffe2::dnnl_graph_static)
    add_library(caffe2::dnnl_graph_static INTERFACE IMPORTED)
  endif()

  set_property(
    TARGET caffe2::dnnl_graph_static PROPERTY INTERFACE_INCLUDE_DIRECTORIES
    ${MKLDNN_INCLUDE_DIR})
  set_property(
    TARGET caffe2::dnnl_graph_static PROPERTY INTERFACE_LINK_LIBRARIES
    ${MKLDNN_LIBRARIES})
endif()
