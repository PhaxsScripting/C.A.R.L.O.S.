import {homedir, hostname} from 'node:os';
import {test,expect} from '@playwright/test';
import {writeFileSync,readFileSync,existsSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
test('real Wayland mouse, keyboard, scroll and drag',async({page,context})=>{
 test.skip(process.env.HOLOHAND_LIVE_INPUT_TEST!=='1','Explicitly enable while the phone is disconnected from Desktop.');
 const cdp=await context.newCDPSession(page);await cdp.send('WebAuthn.enable');await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
 writeFileSync(`${homedir()}/.cache/holohand-browser-tests/pairing.json`,JSON.stringify({hash:createHash('sha256').update('291753').digest('hex'),expires:Date.now()/1000+300}),{mode:0o600});
 await page.goto('http://localhost:8766');await page.getByText('Pair this iPhone',{exact:true}).click();await page.getByLabel('Pairing code').fill('291753');await page.getByRole('button',{name:'Pair with computer'}).click();await expect(page.getByRole('heading',{name:hostname().toUpperCase()})).toBeVisible();
 let win;
 try{
 await page.locator('#desktop-hero').dispatchEvent('click');await page.waitForFunction(()=>[...document.querySelectorAll('#desktop-surface canvas')].some(c=>c.width>100&&c.height>100),{},{timeout:45000});await page.waitForTimeout(1500);
 win=spawn(`${homedir()}/.cache/holohand-input-window`,[],{stdio:'inherit'});await page.waitForTimeout(1000);
 await page.screenshot({path:'test-results/input-window-mobile.png',fullPage:true});
 if(process.env.HOLOHAND_CAPTURE_ONLY)return;
 // Click coordinates are relative to the test window deliberately positioned on eDP-1.
 const box=await page.locator('#desktop-surface').boundingBox();const scale=box.width/1366;
 const point=(x,y)=>({x:box.x+x*scale,y:box.y+y*scale});
 let p=point(260,235);await page.mouse.click(p.x,p.y);
 await page.locator('#phone-keyboard').evaluate(e=>{e.dispatchEvent(new InputEvent('input',{bubbles:true,data:'HOLOHAND INPUT OK',inputType:'insertText'}));});
 await page.mouse.move(p.x,p.y);await page.mouse.wheel(0,150);
 await page.mouse.down();await page.mouse.move(p.x+35,p.y+15,{steps:4});await page.mouse.up();
 await page.mouse.click(p.x,p.y,{button:'right'});await page.getByRole('button',{name:'ESC',exact:true}).click();
 await page.getByRole('button',{name:'CTRL',exact:true}).click();await page.getByRole('button',{name:'/',exact:true}).click();
 await page.waitForTimeout(1000);
 const receipt=JSON.parse(readFileSync('test-results/input-receipt.json','utf8'));expect(receipt.text_matches).toBe(true);expect(receipt.mouse_press).toBe(true);expect(receipt.right_click).toBe(true);expect(receipt.drag).toBe(true);expect(receipt.scroll).toBe(true);expect(receipt.ctrl).toBe(true);
 await page.getByRole('button',{name:'Disconnect',exact:true}).click();
 }finally{win?.kill('SIGTERM');}
});
