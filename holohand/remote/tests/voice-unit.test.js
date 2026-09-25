import {test} from 'node:test';
import assert from 'node:assert/strict';
import {ReplyAudio} from '../web/voice.js';

test('phone playback starts before all chunks are synthesized and stop discards late audio',async()=>{
 const started=[];let end,resolveNext,calls=0;
 globalThis.AudioContext=class {
  resume(){return Promise.resolve();} close(){return Promise.resolve();}
  decodeAudioData(){return Promise.resolve({});}
  createBufferSource(){return {connect(){},start(){started.push(true);end=()=>this.onended();},stop(){this.onended();}};}
 };
 const player=new ReplyAudio();await player.unlock();
 const request=()=>{calls++;return calls===1?Promise.resolve({audio:'AAA='}):new Promise(r=>resolveNext=r);};
 const playing=player.speak('hello '.repeat(35),request);
 await new Promise(r=>setTimeout(r,0));assert.equal(started.length,1);assert.equal(calls,2);
 player.stop();resolveNext({audio:'AAA='});await playing;assert.equal(started.length,1);await player.close();
});
