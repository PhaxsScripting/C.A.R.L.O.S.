// A bounded, explicitly started recording. Raw audio never enters localStorage.
export class ReplyAudio {
 constructor(){this.context=null;this.current=null;this.generation=0;}
 async unlock(){this.context??=new AudioContext();await this.context.resume();}
 stop(){this.generation++;this.current?.stop();this.current=null;}
 async close(){this.stop();await this.context?.close();this.context=null;}
 async speak(text,request){
  this.stop();const generation=this.generation;
  // Bounded chunks start playback before the rest of the answer is synthesized.
  const words=text.replace(/https?:\/\/\S+/g,'the linked page').replace(/[*`#]/g,'').slice(0,1900).split(/\s+/);
  const chunks=[];let chunk='';
  for(const word of words){if(chunk.length+word.length+1>120){if(chunk)chunks.push(chunk);chunk='';}if(word.length>120){chunks.push(...word.match(/.{1,120}/g));}else chunk+=(chunk?' ':'')+word;}if(chunk)chunks.push(chunk);
  let pending=chunks.length?request(chunks[0]):null;
  for(let i=0;i<chunks.length;i++){
   const result=await pending;if(generation!==this.generation)return;
   const p=result.payload||result;if(!p.audio)throw new Error(p.error||'Speech audio is unavailable');
   const bytes=Uint8Array.from(atob(p.audio),c=>c.charCodeAt(0));
   const buffer=await this.context.decodeAudioData(bytes.buffer);if(generation!==this.generation)return;
   // Only one future chunk is in flight. Navigation/Stop invalidates late replies.
   pending=i+1<chunks.length?request(chunks[i+1]):null;
   // Attach rejection immediately, even while the current chunk is playing.
   pending?.catch(()=>{});
   const source=this.context.createBufferSource();source.buffer=buffer;source.connect(this.context.destination);this.current=source;
   await new Promise(resolve=>{source.onended=resolve;source.start();});
   if(this.current===source)this.current=null;if(generation!==this.generation)return;
  }
 }
}

export async function startRecording(onLevel){
 const context=new AudioContext();
 await context.resume();
 let stream,node,source;
 const chunks=[];let length=0,closed=false;
 const close=()=>{if(closed)return;closed=true;stream?.getTracks().forEach(t=>t.stop());node?.disconnect();source?.disconnect();context.close();};
 try{
  stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true},video:false});
  await context.audioWorklet.addModule('/carlos-recorder.js');
  source=context.createMediaStreamSource(stream);node=new AudioWorkletNode(context,'carlos-recorder');
  node.port.onmessage=e=>{if(closed||length>=320000)return;const chunk=e.data;chunks.push(chunk);length+=chunk.length;onLevel(Math.min(20,length/16000));};
  source.connect(node);node.connect(context.destination);
  return {
   cancel(){close();chunks.length=0;},
   stop(){close();const samples=new Int16Array(length);let at=0;for(const chunk of chunks){samples.set(chunk,at);at+=chunk.length;}chunks.length=0;const raw=new Uint8Array(samples.buffer);let binary='';for(let i=0;i<raw.length;i+=8192)binary+=String.fromCharCode(...raw.subarray(i,i+8192));return btoa(binary);}
  };
 }catch(error){close();throw error;}
}
