<?php
// Synthetic transport credentials only. No WordPress, browser or network.
define('ABSPATH',__DIR__.'/');
$GLOBALS['origin']='https://staging3.tonyl36.sg-host.com';$GLOBALS['options']=[];$GLOBALS['filters']=[];
function home_url(){return $GLOBALS['origin'];}
function get_option($key,$default=false){return $GLOBALS['options'][$key]??$default;}
function current_user_can(...$args){return false;}
function get_post_type($id){return 'product';}
function add_action(...$args){}
function add_filter($name,$callback,$priority=10,$args=1){$GLOBALS['filters'][$name][]=[$callback,$priority,$args];}
function apply_filters($name,$value,...$args){foreach($GLOBALS['filters'][$name]??[]as[$fn])$value=$fn($value,...$args);return $value;}
function is_ssl(){return ($_SERVER['HTTPS']??'')==='on';}
function rest_get_url_prefix(){return 'wp-json';}
require dirname(__DIR__).'/hermes-product-companion.php';
use Hermes_Companion_Environment_API as E;
function ok($value,$label){if(!$value)throw new RuntimeException('FAIL '.$label);echo 'PASS '.$label."\n";}
function rejected($fn,$label){try{$fn();}catch(Throwable $e){echo 'PASS '.$label."\n";return;}throw new RuntimeException('FAIL accepted '.$label);}
function configure($role='write',$environment='staging'){
 $token=str_repeat('fixture_',8);
 $GLOBALS['options'][E::CONFIG]=[$role=>['enabled'=>true,'site_origin'=>$GLOBALS['origin'],'environment'=>$environment,'role'=>$role,
  'token_hash'=>hash('sha256',$token),'all_products_read'=>true,'product_ids'=>[9532,6862]]];
 $_SERVER['HTTP_X_HERMES_COMPANION_TOKEN']=$token;
}
function request($uri='/wp-json/hermes-companion/v1/capabilities',$method='GET'){
 $_SERVER=['REQUEST_URI'=>$uri,'REQUEST_METHOD'=>$method,'HTTP_HOST'=>'staging3.tonyl36.sg-host.com','HTTPS'=>'on',
  'HTTP_AUTHORIZATION'=>'Basic '.base64_encode('fixture-preview:synthetic-preview-secret')];
 $_GET=[];parse_str(parse_url($uri,PHP_URL_QUERY)??'',$_GET);configure();
}
function decision($value=true){return apply_filters('application_password_is_api_request',$value);}
request();$before=$_SERVER;
ok(decision()===false,'valid staging role token keeps preview Basic out of WP Application Passwords');
ok($_SERVER===$before,'preview credentials and request headers are not removed or rewritten');
ok(array_keys($GLOBALS['filters'])===['application_password_is_api_request'],'no REST authentication-error bypass is registered');
ok(decision(false)===false&&decision(null)===null,'earlier non-API filter result is preserved');
foreach(['/wp-json/hermes-companion/v1/catalog?page=2&per_page=100',
 '/wp-json/hermes-companion/v1/product-snapshot/9532','/wp-json/hermes-companion/v1/snapshot/6862',
 '/wp-json/hermes-companion/v1/previews/'.str_repeat('a',32),
 '/?rest_route=%2Fhermes-companion%2Fv1%2Fcapabilities',
 '/index.php?rest_route=/hermes-companion/v1/catalog&page=1',
 '/wp-json/hermes-companion/v1/capabilities/']as$uri){request($uri);ok(decision()===false,'exact supported GET route retains scoped token auth');}
foreach(['/wp-json/hermes-companion/v1/previews','/wp-json/hermes-companion/v1/apply/'.str_repeat('a',32),
 '/wp-json/hermes-companion/v1/rollback/'.str_repeat('b',32)]as$uri){request($uri,'POST');ok(decision()===false,'exact write route still requires normal connector permission checks');}
request('/wp-json/hermes-companion/v1/previews','POST');
rejected(fn()=>E::require_write_role(8888),'preview Basic compatibility does not widen the product write allowlist');
foreach([
 '/wp-json/wp/v2/users','/wp-json/wc/v3/orders','/wp-admin/','/xmlrpc.php',
 '/wp-json/hermes-companion/v10/capabilities','/wp-json/hermes-companion/v1evil/capabilities',
 '/wp-json/hermes-companion/v1/capabilities/extra','/wp-json/hermes-companion/v1/unknown',
 '/other/wp-json/hermes-companion/v1/capabilities','//wp-json/hermes-companion/v1/capabilities',
 '/wp-json/hermes-companion/v1/capabilities?rest_route=/wp/v2/users',
 '/?rest_route=/hermes-companion/v1/capabilities&rest_route=/wp/v2/users',
 '/?rest.route=/hermes-companion/v1/capabilities','/?rest_route[]=/hermes-companion/v1/capabilities',
 '/?rest_route=/hermes-companion/v1/capabilities%00/extra','/?rest_route=/hermes-companion/v1/../users',
 '/wp-json/hermes-companion/v1/%63apabilities','/?rest_route=/hermes-companion/v1//capabilities',
 '/not-index.php?rest_route=/hermes-companion/v1/capabilities'
]as$uri){request($uri);ok(decision()===true,'unrelated ambiguous or malformed route retains WP auth behavior');}
request('/wp-json/hermes-companion/v1/apply/'.str_repeat('a',32));ok(decision()===true,'wrong HTTP method retains normal authentication');
request();$_GET['rest_route']='/wp/v2/users';ok(decision()===true,'raw request and parsed REST-route disagreement is rejected');
request();$_SERVER['HTTP_X_HERMES_COMPANION_TOKEN']=str_repeat('invalid_',8);ok(decision()===true,'wrong scoped token cannot suppress WP application-password auth');
request();unset($_SERVER['HTTP_X_HERMES_COMPANION_TOKEN']);ok(decision()===true,'missing token cannot suppress WP application-password auth');
request();$GLOBALS['options'][E::CONFIG]['write']['enabled']=false;ok(decision()===true,'disabled token retains normal auth');
request();$GLOBALS['options'][E::CONFIG]['write']['site_origin']='https://printitprints.com';ok(decision()===true,'copied token from another origin is rejected');
request();configure('read','staging');ok(decision()===true,'unsupported staging read role cannot bypass Basic interpretation');
request();$GLOBALS['origin']='https://printitprints.com';configure('read','production');$_SERVER['HTTP_HOST']='printitprints.com';ok(decision()===true,'production read token leaves application-password auth unchanged');
$GLOBALS['origin']='https://staging3.tonyl36.sg-host.com';
request();$_SERVER['HTTP_HOST']='another.example.com';ok(decision()===true,'request Host must match the bound staging origin');
request();$_SERVER['HTTPS']='off';ok(decision()===true,'unencrypted request does not receive Basic compatibility');
request();unset($_SERVER['HTTP_AUTHORIZATION']);ok(decision()===true,'requests without preview Basic retain ordinary auth');
request();$_SERVER['HTTP_AUTHORIZATION']='Bearer fixture-not-basic';ok(decision()===true,'non-Basic authorization remains unchanged');
request();$_SERVER['REDIRECT_HTTP_AUTHORIZATION']='Basic '.base64_encode('different:synthetic');ok(decision()===true,'conflicting proxy Basic headers are rejected');
request();unset($_SERVER['HTTP_AUTHORIZATION']);$_SERVER['PHP_AUTH_USER']='fixture-preview';$_SERVER['PHP_AUTH_PW']='synthetic-preview-secret';ok(decision()===false,'server-provided PHP Basic auth fields support protected staging');
request();$error=(object)['code'=>'unrelated_existing_auth_error'];
$core_auth=function($input){return decision()===false?$input:(object)['code'=>'invalid_email'];};
ok($core_auth($error)===$error,'disabling only application-password interpretation preserves an earlier auth error');
echo "All protected preview Basic auth tests passed.\n";
