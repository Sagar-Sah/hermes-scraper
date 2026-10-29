<?php
// Protected staging review UI contract. No network or WordPress installation.
define('ABSPATH', __DIR__);
$GLOBALS['root'] = sys_get_temp_dir().'/hermes-review-test-'.bin2hex(random_bytes(6));
mkdir($GLOBALS['root'].'/wcdcp', 0700, true);
$GLOBALS['admin']=true; $GLOBALS['host']='staging3.tonyl36.sg-host.com'; $GLOBALS['options']=[]; $GLOBALS['meta']=[]; $GLOBALS['nonce_checked']=false;
function get_post_type($id){return in_array($id,[9532,6862],true)?'product':false;}
function get_permalink($id){return 'https://staging3.tonyl36.sg-host.com/shop/cards/';}
function home_url($path='/'){return 'https://'.$GLOBALS['host'].$path;}
function current_user_can($cap,$id=null){return $GLOBALS['admin'];}
function get_woocommerce_currency(){return 'USD';}
function wp_upload_dir(){return ['basedir'=>$GLOBALS['root']];}
function wp_json_encode($value,$flags=0){return json_encode($value,$flags);}
function get_post_meta($id,$key,$single=true){return $GLOBALS['meta'][$id][$key]??'';}
function update_post_meta($id,$key,$value){$GLOBALS['meta'][$id][$key]=$value;return true;}
function get_option($key,$default=false){return $GLOBALS['options'][$key]??$default;}
function add_option($key,$value,$deprecated='',$autoload=true){if(isset($GLOBALS['options'][$key]))return false;$GLOBALS['options'][$key]=$value;return true;}
function update_option($key,$value,$autoload=null){$GLOBALS['options'][$key]=$value;return true;}
function wp_parse_url($value,$component=-1){return parse_url($value,$component);}
function clean_post_cache($id){}
function wc_delete_product_transients($id){}
function do_action($hook,...$args){}
function add_action($hook,$callback,...$args){}
function wp_unslash($value){return $value;}
function check_admin_referer($action){if(($_POST['_wpnonce']??'')!=='valid')throw new RuntimeException('Nonce rejected');$GLOBALS['nonce_checked']=true;}
function wp_nonce_field($action){echo '<input name="_wpnonce" value="valid">';}
function esc_html($value){return htmlspecialchars((string)$value,ENT_QUOTES);}
function esc_attr($value){return esc_html($value);}
function esc_textarea($value){return esc_html($value);}
function esc_url($value){return esc_html($value);}
require dirname(__DIR__).'/hermes-product-companion.php';
use Hermes_Product_Companion as C;
use Hermes_Companion_Staging_Review as R;
function ok($value,$label){if(!$value)throw new RuntimeException('FAIL '.$label);echo 'PASS '.$label."\n";}
function rejects($fn,$label){try{$fn();}catch(Throwable $e){echo 'PASS '.$label."\n";return;}throw new RuntimeException('FAIL accepted '.$label);}
$path=$GLOBALS['root'].'/wcdcp/9532-printit.csv';
$original="SIZE,RunSize,UnitPrice,TotalPrice\r\n2x3.5,100,0.15,15.00\r\n";
file_put_contents($path,$original);
try {
 $GLOBALS['options'][C::CONFIG]=['product_ids'=>[9532,6862],'enabled'=>true,'token_hash'=>str_repeat('f',64)];
 ok(R::authorize(9532)===9532,'configured staging product is authorized');
 $GLOBALS['host']='printitprints.com';rejects(fn()=>R::authorize(9532),'production cannot run staging review');$GLOBALS['host']=R::HOST;
 $GLOBALS['options'][C::CONFIG]['product_ids']=[6862];rejects(fn()=>R::authorize(9532),'review respects connector product allowlist');$GLOBALS['options'][C::CONFIG]['product_ids']=[9532,6862];
 $GLOBALS['admin']=false;rejects(fn()=>R::authorize(9532),'review requires signed-in Woo administration');$GLOBALS['admin']=true;
 $snapshot=R::request(['product_id'=>'9532','review_action'=>'snapshot'])['snapshot'];
 ok($snapshot['csv_sha256']===hash('sha256',$original)&&$snapshot['row_count']===1&&!isset($snapshot['rows']),'snapshot summary proves bytes without exposing full cost rows');
 $source=['audit_id'=>str_repeat('a',32),'evidence'=>['verified'=>true,'source_observed_at'=>time(),'sha256'=>str_repeat('b',64)],'changes'=>[['kind'=>'add_document','url'=>'https://4over.com/media/asset/templates/card.eps','label'=>'Artwork template']]];
 $input=['product_id'=>'9532','review_action'=>'preview','expected_revision'=>$snapshot['revision'],'source_payload'=>json_encode($source)];
 $preview=R::request($input)['preview'];ok(file_get_contents($path)===$original&&$preview['diff'][0]['kind']==='add_document','visible preview does not change source cost CSV');
 $applied=R::request(['product_id'=>'9532','review_action'=>'apply','expected_revision'=>$snapshot['revision'],'preview_id'=>$preview['preview_id']]);
 ok($applied['receipt']['status']==='applied'&&count($applied['snapshot']['documents'])===1&&$applied['snapshot']['csv_sha256']===$snapshot['csv_sha256'],'admin apply adds reviewed link while CSV bytes stay exact');
 ok(!str_contains(json_encode($applied),'before_csv')&&!str_contains(json_encode($applied),'token_hash'),'receipt excludes private backups and connection secrets');
 $restored=R::request(['product_id'=>'9532','review_action'=>'rollback','expected_revision'=>$applied['snapshot']['revision'],'change_id'=>$applied['receipt']['change_id']]);
 ok($restored['receipt']['status']==='rolled_back'&&$restored['snapshot']['revision']===$snapshot['revision'],'admin rollback restores exact original revision');
 rejects(fn()=>R::request(['product_id'=>'6862','review_action'=>'apply','expected_revision'=>$snapshot['revision'],'preview_id'=>$preview['preview_id']]),'cross-product preview reuse is rejected');
 $source['token']='never-allowed';$input['source_payload']=json_encode($source);rejects(fn()=>R::request($input),'payload cannot carry connection secrets or extra fields');unset($source['token']);
 $_SERVER['REQUEST_METHOD']='POST';$_POST=['product_id'=>'9532','review_action'=>'snapshot'];rejects(fn()=>R::render(),'admin form POST requires valid nonce');
 $_POST['_wpnonce']='valid';ob_start();R::render();$html=ob_get_clean();ok($GLOBALS['nonce_checked']&&str_contains($html,'Verified server result')&&str_contains($html,'Create preview'),'review form verifies nonce and renders visible results/actions');
 echo "All admin review tests passed.\n";
} finally {
 foreach(glob($GLOBALS['root'].'/wcdcp/*') as $file)unlink($file);
 foreach(glob($GLOBALS['root'].'/wcdcp/.*') as $file)if(is_file($file))unlink($file);
 rmdir($GLOBALS['root'].'/wcdcp');rmdir($GLOBALS['root']);
}
