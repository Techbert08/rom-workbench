# Idempotently apply a ;-separated list of git patches in the current source dir.
# For each: if it reverse-applies cleanly it's already applied → skip; else apply.
# Invoked as: cmake -DPATCHES="a.patch;b.patch" -P apply_patches.cmake
foreach(patch ${PATCHES})
  execute_process(
    COMMAND git apply --reverse --check ${patch}
    RESULT_VARIABLE already_applied
    OUTPUT_QUIET ERROR_QUIET)
  if(NOT already_applied EQUAL 0)
    execute_process(
      COMMAND git apply ${patch}
      RESULT_VARIABLE rc)
    if(NOT rc EQUAL 0)
      message(FATAL_ERROR "Failed to apply patch: ${patch}")
    endif()
    message(STATUS "Applied patch: ${patch}")
  else()
    message(STATUS "Patch already applied: ${patch}")
  endif()
endforeach()
