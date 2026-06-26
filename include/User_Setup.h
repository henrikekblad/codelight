// GeekMagic Ultra – ST7789V 240x240 pin configuration for TFT_eSPI
#define ST7789_DRIVER
#define TFT_WIDTH  240
#define TFT_HEIGHT 240

#define TFT_MOSI   13
#define TFT_SCLK   14
#define TFT_CS     -1   // CS tied low on PCB (no CS pin needed)
#define TFT_DC      0   // GPIO0 – confirmed by ESPHome config
#define TFT_RST     2   // GPIO2 – confirmed by ESPHome config
#define TFT_BL      5

#define TFT_BACKLIGHT_ON LOW   // active LOW – confirmed by ESPHome config
#define TFT_SPI_MODE SPI_MODE3 // required for this display – confirmed by ESPHome config

// ST7789V 240x240 square panels almost always need inversion
#define TFT_INVERSION_ON

// Colour order – swap to TFT_BGR if red/blue are swapped
#define TFT_RGB_ORDER TFT_BGR

#define LOAD_GLCD
#define LOAD_FONT2
#define LOAD_FONT4
#define LOAD_FONT6
#define LOAD_GFXFF

#define SMOOTH_FONT

#define SPI_FREQUENCY  20000000
#define SPI_READ_FREQUENCY  6000000
