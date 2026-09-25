import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'tests',testMatch:'*.spec.js',workers:1,timeout:60000,webServer:{command:'.venv/bin/python tests/serve.py',url:'http://localhost:8766/api/health',reuseExistingServer:false},use:{baseURL:'http://localhost:8766',viewport:{width:390,height:844},deviceScaleFactor:1,hasTouch:true,isMobile:true},reporter:'list'});
