const CACHE='holohand-shell-v1';
const SHELL=['/','/manifest.webmanifest','/icon-180.png','/icon-192.png','/icon-512.png'];
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL))));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin||u.pathname.startsWith('/api/')||u.pathname.startsWith('/ws/'))return;e.respondWith(fetch(e.request).then(res=>{if(res.ok&&(u.pathname.startsWith('/assets/')||SHELL.includes(u.pathname))){const copy=res.clone();caches.open(CACHE).then(c=>c.put(e.request,copy));}return res;}).catch(async()=>await caches.match(e.request)||(e.request.mode==='navigate'?await caches.match('/'):Response.error())));});
self.addEventListener('push',e=>{const d=e.data?.json()||{};e.waitUntil(self.registration.showNotification(d.title||'HoloHand',{body:d.body||'An update is available.',icon:'/icon-192.png',data:{url:'/'}}));});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(clients.openWindow('/'));});
