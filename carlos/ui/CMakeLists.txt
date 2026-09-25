cmake_minimum_required(VERSION 3.21)
project(ev-ui VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.6 REQUIRED COMPONENTS Core Gui Qml Quick QuickControls2 Network Widgets)
# The voice HUD uses a compositor-enforced non-focusable layer on KDE Wayland.
# Its QML runtime module is supplied by this existing Plasma component.
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    find_package(LayerShellQt 6.6 REQUIRED)
endif()
qt_standard_project_setup(REQUIRES 6.6)
qt_policy(SET QTP0001 NEW)

set_source_files_properties(qml/Main.qml PROPERTIES QT_RESOURCE_ALIAS Main.qml)
set_source_files_properties(qml/CoreGraph.qml PROPERTIES QT_RESOURCE_ALIAS CoreGraph.qml)
set_source_files_properties(qml/MetricCard.qml PROPERTIES QT_RESOURCE_ALIAS MetricCard.qml)
set_source_files_properties(qml/BrainView.qml PROPERTIES QT_RESOURCE_ALIAS BrainView.qml)
set_source_files_properties(qml/BrainExplorer.qml PROPERTIES QT_RESOURCE_ALIAS BrainExplorer.qml)
set_source_files_properties(qml/DailyView.qml PROPERTIES QT_RESOURCE_ALIAS DailyView.qml)
foreach(component HudPanel SciButton SciField HudBackdrop NeuralHologram AgentOrb)
    set_source_files_properties(qml/${component}.qml PROPERTIES QT_RESOURCE_ALIAS ${component}.qml)
endforeach()
set_source_files_properties(assets/ev-neural-brain.png PROPERTIES QT_RESOURCE_ALIAS assets/ev-neural-brain.png)

qt_add_executable(ev-ui
    src/main.cpp
    src/EvClient.cpp
    src/EvClient.h
)

set(EV_HUD_SOURCE "${CMAKE_CURRENT_SOURCE_DIR}/qml/HudWindow.qml")
if(CMAKE_SYSTEM_NAME STREQUAL "FreeBSD")
    configure_file(platform/FreeBSDHudWindow.qml "${CMAKE_CURRENT_BINARY_DIR}/HudWindow.qml" COPYONLY)
    set(EV_HUD_SOURCE "${CMAKE_CURRENT_BINARY_DIR}/HudWindow.qml")
endif()
set_source_files_properties(${EV_HUD_SOURCE} PROPERTIES QT_RESOURCE_ALIAS HudWindow.qml)
qt_add_qml_module(ev-ui
    URI EV.ControlCenter
    VERSION 1.0
    QML_FILES
        ${EV_HUD_SOURCE}
        qml/Main.qml
        qml/CoreGraph.qml
        qml/MetricCard.qml
        qml/BrainView.qml
        qml/BrainExplorer.qml
        qml/DailyView.qml
        qml/HudPanel.qml
        qml/SciButton.qml
        qml/SciField.qml
        qml/HudBackdrop.qml
        qml/NeuralHologram.qml
        qml/AgentOrb.qml
    RESOURCES
        assets/ev-neural-brain.png
)

target_link_libraries(ev-ui PRIVATE
    Qt6::Core Qt6::Gui Qt6::Qml Qt6::Quick Qt6::QuickControls2 Qt6::Network Qt6::Widgets
)

install(TARGETS ev-ui RUNTIME DESTINATION bin)

include(CTest)
if(BUILD_TESTING)
    find_package(Qt6 REQUIRED COMPONENTS Test)
    qt_add_executable(ev-client-tests ../tests/test_ev_client.cpp src/EvClient.cpp src/EvClient.h)
    target_include_directories(ev-client-tests PRIVATE src)
    target_link_libraries(ev-client-tests PRIVATE Qt6::Core Qt6::Network Qt6::Test)
    add_test(NAME ev-client-tests COMMAND ev-client-tests)
    find_program(EV_QML_TEST_RUNNER qmltestrunner HINTS /usr/lib64/qt6/bin /usr/lib/qt6/bin /usr/local/lib/qt6/bin)
    if(EV_QML_TEST_RUNNER)
        add_test(NAME ev-interface-tests COMMAND ${EV_QML_TEST_RUNNER} -input ${CMAKE_CURRENT_SOURCE_DIR}/../tests/qml -platform offscreen)
        set_tests_properties(ev-interface-tests PROPERTIES ENVIRONMENT "QT_QUICK_BACKEND=software;QT_QUICK_CONTROLS_STYLE=Basic" TIMEOUT 45)
    endif()
endif()
