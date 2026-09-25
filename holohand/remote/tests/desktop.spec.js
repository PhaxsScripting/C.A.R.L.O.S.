import {homedir, hostname} from 'node:os';
import {test,expect} from '@playwright/test';
import {writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
test('real KDE desktop stream',async({page,context})=>{
 test.skip(process.env.HOLOHAND_LIVE_DESKTOP_TEST!=='1','Explicitly enable while the phone is disconnected from Desktop.');
 const cdp=await context.newCDPSession(page);await cdp.send('WebAuthn.enable');await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
 writeFileSync(`${homedir()}/.cache/holohand-browser-tests/pairing.json`,JSON.stringify({hash:createHash('sha256').update('826317').digest('hex'),expires:Date.now()/1000+300}),{mode:0o600});
 await page.goto('/');await page.getByText('Pair this iPhone',{exact:true}).click();await page.getByLabel('Pairing code').fill('826317');await page.getByLabel('Device name').fill('Desktop stream test');await page.getByRole('button',{name:'Pair with computer'}).click();await expect(page.getByRole('heading',{name:hostname().toUpperCase()})).toBeVisible();
 await page.locator('#desktop-hero').click();
 await page.waitForFunction(()=>[...document.querySelectorAll('#desktop-surface canvas')].some(c=>c.width>100&&c.height>100),{},{timeout:45000});await page.waitForTimeout(3000);await page.screenshot({path:'test-results/live-desktop.png',fullPage:true});
 const size=await page.locator('#desktop-surface').evaluate(e=>{const c=[...e.querySelectorAll('canvas')].find(c=>c.width>100);return {width:c.width,height:c.height};});expect(size.width).toBeGreaterThan(100);expect(size.height).toBeGreaterThan(100);
 await page.getByRole('button',{name:'Disconnect',exact:true}).click();await expect(page.getByRole('heading',{name:hostname().toUpperCase()})).toBeVisible();
});
