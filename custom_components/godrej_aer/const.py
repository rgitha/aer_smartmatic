"""Constants for the Godrej Aer Smart Matic integration."""

DOMAIN = "godrej_aer"

CONNECTION_TIMEOUT = 120

CONF_COMMAND_CHAR = "command_char"
CONF_REPLY_CHAR = "reply_char"
# Whether the device keeps time (found at setup; entries from before are
# taken to). Without a clock it has no schedules.
CONF_HAS_CLOCK = "has_clock"
