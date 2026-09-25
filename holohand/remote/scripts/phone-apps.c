#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <libimobiledevice/libimobiledevice.h>
#include <libimobiledevice/lockdown.h>
#include <libimobiledevice/installation_proxy.h>
#include <plist/plist.h>
int main(void){idevice_t dev=NULL;lockdownd_client_t lc=NULL;lockdownd_service_descriptor_t service=NULL;instproxy_client_t client=NULL;plist_t result=NULL;
if(idevice_new(&dev,NULL)!=IDEVICE_E_SUCCESS)return 1;
if(lockdownd_client_new_with_handshake(dev,&lc,"HoloHand")!=LOCKDOWN_E_SUCCESS)return 2;
if(lockdownd_start_service(lc,"com.apple.mobile.installation_proxy",&service)!=LOCKDOWN_E_SUCCESS)return 3;
if(instproxy_client_new(dev,service,&client)!=INSTPROXY_E_SUCCESS)return 4;
plist_t opts=instproxy_client_options_new();instproxy_client_options_add(opts,"ApplicationType","User",NULL);
if(instproxy_browse(client,opts,&result)!=INSTPROXY_E_SUCCESS)return 5;
int found=0;for(unsigned i=0;i<plist_array_get_size(result);i++){plist_t app=plist_array_get_item(result,i);char *id=NULL;plist_get_string_val(plist_dict_get_item(app,"CFBundleIdentifier"),&id);if(id&&strstr(id,"tailscale")){printf("Tailscale installed: %s\n",id);found=1;}free(id);}if(!found)puts("Tailscale not found among installed user applications.");
plist_free(result);instproxy_client_options_free(opts);instproxy_client_free(client);lockdownd_service_descriptor_free(service);lockdownd_client_free(lc);idevice_free(dev);return 0;}
