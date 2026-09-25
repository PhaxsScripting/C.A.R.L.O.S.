// Worklet produces mono 16 kHz signed PCM; it never plays microphone audio.
class CarlosRecorder extends AudioWorkletProcessor {
 constructor(){super();this.total=0;this.count=0;this.phase=0;this.samples=[];}
 process(inputs,outputs){
  const data=inputs[0]?.[0];
  if(data)for(const sample of data){
   this.total+=sample;this.count++;this.phase+=16000;
   if(this.phase>=sampleRate){
    this.phase-=sampleRate;
    this.samples.push(Math.round(Math.max(-1,Math.min(1,this.total/this.count))*32767));
    this.total=0;this.count=0;
    if(this.samples.length===320){const chunk=new Int16Array(this.samples);this.port.postMessage(chunk,[chunk.buffer]);this.samples=[];}
   }
  }
  for(const output of outputs)for(const channel of output)channel.fill(0);
  return true;
 }
}
registerProcessor('carlos-recorder',CarlosRecorder);
