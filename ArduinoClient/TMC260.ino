
const __FlashStringHelper* setCurrent(unsigned char motor_number, int newCurrent) {
  if (newCurrent>MAX_MOTOR_CURRENT) {
    return F("Current too high");
  } 
  motors[motor_number].tmc260.setCurrent(newCurrent);
  setTMC260Registers(motor_number);
  return NULL;
}


void intializeTMC260() {
  for (char i=0; i<nr_of_motors;i++) {
    unsigned char cs_pin = motors[i].cs_pin;
    //configure TMC43x SPI
    write43x(cs_pin, SPIOUT_CONF_REGISTER,TMC_26X_CONFIG);
    //configure the TMC26x
    motors[i].tmc260.setMicrosteps(256);
    setTMC260Registers(i);
  }
}

void setTMC260Registers(unsigned char motor_number) {
  set260Register(motors[motor_number].cs_pin,motors[motor_number].tmc260.getDriverControlRegisterValue());
  set260Register(motors[motor_number].cs_pin,motors[motor_number].tmc260.getChopperConfigRegisterValue());
  set260Register(motors[motor_number].cs_pin,motors[motor_number].tmc260.getStallGuard2RegisterValue());
  set260Register(motors[motor_number].cs_pin,motors[motor_number].tmc260.getDriverConfigurationRegisterValue() | 0x80);
}


void set260Register(unsigned char cs_pin, unsigned long value) {
  //santitize to 20 bits 
  value &= 0xFFFFF;
  write43x(cs_pin, COVER_LOW_REGISTER,value);  //Cover-Register: Einstellung des SMARTEN=aus

  read43x(cs_pin, STATUS_REGISTER,0x0); //Abfrage Status, um SPI-Transfer zu beenden
  read43x(cs_pin, STATUS_REGISTER,0x0); //Abfrage Status, um SPI-Transfer zu beenden
  read43x(cs_pin, STATUS_REGISTER,0x0); //Abfrage Status, um SPI-Transfer zu beenden
}





