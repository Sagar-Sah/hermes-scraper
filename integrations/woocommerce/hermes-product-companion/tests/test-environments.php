<?php
// No WordPress or network. Exercises separate roles, legacy coexistence and adapters.
define('ABSPATH',__DIR__.'/');
$GLOBALS['root']=sys_get_temp_dir().'/hermes-env-'.bin2hex(random_bytes(8));mkdir($GLOBALS['root'].'/wcdcp',0700,true);
$GLOBALS['options']=[];$GLOBALS['meta']=[];$GLOBALS['origin']='https://printitprints.com';$GLOBALS['admin']=false;$GLOBALS['routes']=[];
function get_post_type($id){return in_array((int)$id,[9532,18554,6383,17783,8888],true)?'product':false;}
function get_permalink($id){return $GLOBALS['origin'].'/shop/product-'.$id.'/';}
function current_user_can($cap,$id=null){return $GLOBALS['admin'];}
function get_woocommerce_currency(){return 'USD';}
function wp_upload_dir(){return ['basedir'=>$GLOBALS['root']];}
function wp_json_encode($v){return json_encode($v,JSON_UNESCAPED_SLASHES);}
function get_post_meta($id,$key,$single=true){return $GLOBALS['meta'][$id][$key]??'';}
function update_post_meta($id,$key,$v){$GLOBALS['meta'][$id][$key]=$v;return true;}
function get_option($key,$default=false){return $GLOBALS['options'][$key]??$default;}
function add_option($key,$v,...$args){if(isset($GLOBALS['options'][$key]))return false;$GLOBALS['options'][$key]=$v;return true;}
function update_option($key,$v,...$args){$GLOBALS['options'][$key]=$v;return true;}
function home_url(){return $GLOBALS['origin'];}
function wp_parse_url($v){return parse_url($v);}
function clean_post_cache($id){}
function wc_delete_product_transients($id){}
function do_action(...$args){}
function add_action(...$args){}
function register_rest_route($ns,$path,$def){$GLOBALS['routes'][$path]=$def;}
function rest_ensure_response($v){return $v;}
function apply_filters($name,$value,...$args){return $value;}
function wp_get_custom_css(){return '.example {color:#123456}';}
function get_posts($args){return [];}
function wc_get_products($q){$GLOBALS['inventory_query']=$q;return (object)['products'=>[9532,18554],'total'=>201,'max_num_pages'=>3];}
function wc_get_product($id){return new class($id){function __construct(private $id){} function get_data(){return ['name'=>'Product'.$this->id,'description'=>'Description','price'=>'27.75','weight'=>'2','customer_secret'=>'never returned'];}function get_attributes(){return [];}function get_name(){return 'Product'.$this->id;}function get_status(){return 'publish';}};}
class PILA_Model {static function profile($id){return $id===18554?['fields'=>['material'=>['options'=>['vinyl'=>'Vinyl']]],'source_url'=>'https://www.b2sign.com/example','rate'=>4.125,'private_token'=>'not exported']:null;}}
class PIGF_Configurator {private const COROPLAST_ID=6383;static function banner_profiles(){return [17783=>['supplier'=>'b2sign','min_width'=>12,'max_width'=>120]];}}
class WP_Error {function __construct(...$args){}}
require dirname(__DIR__).'/hermes-product-companion.php';
use Hermes_Product_Companion as C;
use Hermes_Companion_Environment_API as E;
function ok($v,$m){if(!$v)throw new RuntimeException('FAIL '.$m);echo 'PASS '.$m."\n";}
function rejects($f,$m){try{$f();}catch(Throwable $e){echo 'PASS '.$m."\n";return;}throw new RuntimeException('FAIL accepted '.$m);}
function connection($role,$environment,$ids=[]){$token=str_repeat(substr($role,0,1),64);$GLOBALS['options'][E::CONFIG]=[$role=>['enabled'=>true,'environment'=>$environment,'role'=>$role,'site_origin'=>$GLOBALS['origin'],'token_hash'=>hash('sha256',$token),'all_products_read'=>true,'product_ids'=>$ids]];$_SERVER['HTTP_X_HERMES_COMPANION_TOKEN']=$token;}
function request_input($revision){return ['product_id'=>9532,'expected_revision'=>$revision,'audit_id'=>str_repeat('a',32),'evidence'=>['verified'=>true,'source_observed_at'=>time(),'sha256'=>str_repeat('b',64)],'changes'=>[['kind'=>'set_cost','selection'=>['size'=>'2 x 3.5','quantity'=>'100'],'cost'=>'17.25']]];}
$original="SIZE,Turnaround,RunSize,UnitPrice,TotalPrice\n2 x 3.5,4 Business Days,100,0.15,15.00\n";
file_put_contents($GLOBALS['root'].'/wcdcp/9532-printit.csv',$original);
file_put_contents($GLOBALS['root'].'/wcdcp/6383-printit.csv',$original);
file_put_contents($GLOBALS['root'].'/wcdcp/18554-printit.csv',$original);
try {
 connection('read','production');
 ok(E::capabilities()['role']==='read'&&E::capabilities()['environment']==='production','read connection bound to production origin');
 $s=E::snapshot(9532);ok($s['calculator']['family']==='custom_csv'&&$s['rows'][0]['cost']==='15.00','source CSV snapshot remains source cost');
 rejects(fn()=>C::preview(request_input($s['revision'])),'production read token cannot create write preview');
 ok(E::snapshot(18554)['calculator']['family']==='pila','PILA takes precedence over an obsolete CSV');
 ok(E::snapshot(6383)['calculator']['family']==='pigf'&&!E::snapshot(6383)['calculator']['write_supported'],'PIGF override cannot be falsely treated as writable CSV');
 ok(E::snapshot(17783)['calculator']['profile']['supplier']==='b2sign','PIGF banner config readable without source requests');
 ok(!str_contains(json_encode(E::snapshot(18554)),'private_token'),'profile snapshot excludes secret configuration keys');
 ok(E::snapshot(8888)['calculator']['family']==='woocommerce'&&E::snapshot(8888)['calculator']['coverage_complete']===false,'unknown custom coverage never becomes complete');
 $list=E::inventory(2);ok($list['next_cursor']===3&&$list['total']===201&&$GLOBALS['inventory_query']['page']===2,'inventory paginates beyond first100 products');
 $baseline=E::environment_snapshot();ok($baseline['custom_css']['sha256']===hash('sha256',wp_get_custom_css())&&$baseline['staging_isolation']['outbound_mail']==='unknown','baseline fingerprints actual CSS and leaves isolation unknown');
 ok(count($baseline['custom_calculator_files']['files'])===3&&!str_contains(json_encode($baseline),'15.00'),'baseline returns hashes without source-cost rows');
 $GLOBALS['origin']='https://staging3.tonyl36.sg-host.com';ok(E::credential()===false,'copied production token is invalid on staging');
 connection('write','staging');
 rejects(fn()=>C::preview(request_input(C::snapshot(9532)['revision'])),'staging all-read scope does not grant unlisted product writes');
 connection('write','staging',[9532,18554]);
 rejects(fn()=>E::require_write_role(18554),'unsupported family stays blocked even when product write-scoped');
 $s=C::snapshot(9532);$p=C::preview(request_input($s['revision']));$a=C::apply($p['preview_id'],['expected_revision'=>$s['revision']]);
 ok(C::snapshot(9532)['rows'][0]['cost']==='17.25','scoped staging custom CSV repair applies');
 C::rollback($a['change_id'],['expected_revision'=>$a['revision']]);
 $GLOBALS['origin']='https://printitprints.com';connection('release','production',[9532]);
 rejects(fn()=>C::preview(request_input(C::snapshot(9532)['revision'])),'production release token requires manual manifest proof');
 $_SERVER['HTTP_X_HERMES_RELEASE_MANIFEST']=str_repeat('c',64);$_SERVER['HTTP_X_HERMES_RELEASE_APPROVAL']=str_repeat('d',32);
 $s=C::snapshot(9532);$p=C::preview(request_input($s['revision']));
 $_SERVER['HTTP_X_HERMES_RELEASE_MANIFEST']=str_repeat('e',64);
 rejects(fn()=>C::apply($p['preview_id'],['expected_revision'=>$s['revision']]),'release apply cannot substitute a different approved manifest');
 $_SERVER['HTTP_X_HERMES_RELEASE_MANIFEST']=str_repeat('c',64);$a=C::apply($p['preview_id'],['expected_revision'=>$s['revision']]);
 ok(C::snapshot(9532)['rows'][0]['cost']==='17.25','manually approved release uses exact previewed data');
 C::rollback($a['change_id'],['expected_revision'=>$a['revision']]);ok(file_get_contents($GLOBALS['root'].'/wcdcp/9532-printit.csv')===$original,'production rollback retains original source bytes');
 E::routes();ok(count($GLOBALS['routes'])===5,'five new readonly routes coexist with five legacy routes');
 echo "All environment connector tests passed.\n";
} finally {
 foreach(glob($GLOBALS['root'].'/wcdcp/{*,.*}',GLOB_BRACE)as$p)if(is_file($p))unlink($p);
 rmdir($GLOBALS['root'].'/wcdcp');rmdir($GLOBALS['root']);
}
