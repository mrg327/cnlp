# blinky

Toggles the LED and reports the button. Pins come only from `spec.cnl`.

    cnlp gen examples/blinky      # writes components/board/
    cd examples/blinky && idf.py set-target esp32 && idf.py build

App code uses `PIN_LED` / `PIN_BUTTON` from the generated `board_pins.h`.
