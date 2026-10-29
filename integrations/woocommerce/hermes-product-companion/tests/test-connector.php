<?php
// Run with: php tests/test-connector.php. No WordPress, browser or network needed.
define('ABSPATH',__DIR__);
$GLOBALS['root']=sys_get_temp_dir().'/hermes-woo-test-'.bin2hex(random_bytes(8));
mkdir($GLOBALS['root'].'/wcdcp',0700,true);
$GLOBALS['options']=[];$GLOBALS['meta']=[];$GLOBALS['admin']=true;$GLOBALS['fail_meta']=false;$GLOBALS['hooks']=[];
function get_post_type($id){return $id===9532?'product':false;}
function get_permalink($id){return 'https://printitprints.com/shop/cards/';}
function current_user_can($cap,$id=null){return $GLOBALS['admin'];}
function get_woocommerce_currency(){return 'USD';}
function wp_upload_dir(){return ['basedir'=>$GLOBALS['root']];}
function wp_json_encode($value){return json_encode($value,JSON_UNESCAPED_SLASHES);}
function get_post_meta($id,$key,$single=true){return $GLOBALS['meta'][$id][$key]??'';}
function update_post_meta($id,$key,$value){if($GLOBALS['fail_meta'])return false;$GLOBALS['meta'][$id][$key]=$value;return true;}
function get_option($key,$default=false){return $GLOBALS['options'][$key]??$default;}
function add_option($key,$value,$deprecated='',$autoload=true){if(array_key_exists($key,$GLOBALS['options']))return false;$GLOBALS['options'][$key]=$value;return true;}
function update_option($key,$value,$autoload=null){$GLOBALS['options'][$key]=$value;return true;}
function wp_parse_url($value){return parse_url($value);}
function clean_post_cache($id){}
function wc_delete_product_transients($id){}
function do_action($hook,...$args){$GLOBALS['hooks'][]=[$hook,$args];}
function add_action($hook,$callback,...$args){}
function register_rest_route($namespace,$path,$definition){$GLOBALS['routes'][$path]=$definition;}
function rest_ensure_response($value){return $value;}
class WP_Error {public function __construct(public $code,public $message,public $data){}}
require dirname(__DIR__).'/hermes-product-companion.php';
use Hermes_Product_Companion as C;
function ok($value,$message){if(!$value)throw new RuntimeException('FAIL: '.$message);echo 'PASS '.$message."\n";}
function rejects($callback,$message){try{$callback();}catch(Throwable $e){echo 'PASS '.$message." (".$e->getMessage().")\n";return;}throw new RuntimeException('FAIL accepted: '.$message);}
function input($revision,$changes){return ['product_id'=>9532,'expected_revision'=>$revision,'audit_id'=>str_repeat('a',32),'evidence'=>['verified'=>true,'source_observed_at'=>time(),'sha256'=>str_repeat('b',64)],'changes'=>$changes];}
function selection($quantity='100'){return ['size'=>'2 x 3.5','stock'=>'Kraft','quantity'=>$quantity];}
$path=$GLOBALS['root'].'/wcdcp/9532-printit.csv';
$original="SIZE,STOCK,Turnaround,RunSize,UnitPrice,TotalPrice\r\n\"2 x 3.5\",Kraft,\"4 Business Days\",100,0.15,15.00\r\n";
file_put_contents($path,$original);
try {
 $snapshot=C::snapshot(9532);
 ok($snapshot['row_count']===1&&$snapshot['rows'][0]['selection']===selection(),'snapshot reads exact local CSV and normalized quantities');
 ok($snapshot['rows'][0]['cost']==='15.00'&&$snapshot['retail_rule']['status']==='unknown','supplier cost and unverified rule kept explicit');
 rejects(fn()=>C::preview(input($snapshot['revision'],[['kind'=>'set_cost','selection'=>selection(),'production'=>'4 Business Days','cost'=>'15']])), 'no-op changes rejected');
 $bad=input($snapshot['revision'],[['kind'=>'set_cost','selection'=>selection(),'production'=>'4 Business Days','cost'=>'17']]);$bad['evidence']['verified']=false;
 rejects(fn()=>C::preview($bad),'unverified source evidence rejected');
 $bad['evidence']['verified']=true;$bad['evidence']['source_observed_at']=time()-21601;
 rejects(fn()=>C::preview($bad),'stale source evidence rejected');
 foreach(['set_retail','delete_row','set_description'] as $kind)rejects(fn()=>C::preview(input($snapshot['revision'],[['kind'=>$kind]])),'unsafe change '.$kind.' rejected');
 $change=['kind'=>'set_cost','selection'=>selection(),'production'=>'4 Business Days','cost'=>'17.25'];
 $preview=C::preview(input($snapshot['revision'],[$change]));
 ok(file_get_contents($path)===$original,'preview leaves calculator file unchanged');
 ok($preview['diff'][0]['before']['TotalPrice']==='15.00'&&$preview['diff'][0]['after']['TotalPrice']==='17.25','preview contains before and after source cost');
 rejects(fn()=>C::apply($preview['preview_id'],['expected_revision'=>str_repeat('0',64)]),'apply stale revision rejected');
 $applied=C::apply($preview['preview_id'],['expected_revision'=>$snapshot['revision']]);
 $after=C::snapshot(9532);
 ok($applied['status']==='applied'&&$applied['revision']===$after['revision']&&$after['rows'][0]['cost']==='17.25','apply writes exactly previewed source cost');
 ok($after['rows'][0]['raw']['UnitPrice']==='0.1725','unit cost is recalculated from order quantity');
 ok(C::apply($preview['preview_id'],['expected_revision'=>$snapshot['revision']])['change_id']===$applied['change_id'],'apply retry is idempotent');
 $prepared=C::load('change',$applied['change_id']);$prepared['status']='prepared';C::update('change',$applied['change_id'],$prepared);
 ok(C::apply($preview['preview_id'],['expected_revision'=>$snapshot['revision']])['status']==='applied','crash after write settles prepared receipt without writing again');
 $status=C::preview_status($preview['preview_id']);
 ok($status['change']['change_id']===$applied['change_id']&&!isset($status['change']['before_csv']),'preview recovery endpoint returns receipt without private backup bytes');
 $backup=C::load('change',$applied['change_id']);
 ok(base64_decode($backup['before_csv'])===$original,'private change record backs up exact original bytes before write');
 file_put_contents($path,str_replace('17.25','18.25',file_get_contents($path)));
 rejects(fn()=>C::rollback($applied['change_id'],['expected_revision'=>C::snapshot(9532)['revision']]),'rollback refuses an intervening manual edit');
 file_put_contents($path,base64_decode($backup['after_csv']));
 $rolled=C::rollback($applied['change_id'],['expected_revision'=>$after['revision']]);
 ok($rolled['status']==='rolled_back'&&file_get_contents($path)===$original,'rollback restores exact bytes');
 ok(C::rollback($applied['change_id'],['expected_revision'=>$after['revision']])['status']==='rolled_back','rollback retry idempotent');
 $prepared=C::load('change',$applied['change_id']);$prepared['status']='prepared';C::update('change',$applied['change_id'],$prepared);
 ok(C::apply($preview['preview_id'],['expected_revision'=>$snapshot['revision']])['change_id']===$applied['change_id'],'crash before write resumes the same prepared backup');
 C::rollback($applied['change_id'],['expected_revision'=>C::snapshot(9532)['revision']]);
 $noProduction=$change;unset($noProduction['production']);$noProduction['cost']='18.50';
 $noProductionPreview=C::preview(input(C::snapshot(9532)['revision'],[$noProduction]));
 $noProductionApply=C::apply($noProductionPreview['preview_id'],['expected_revision'=>C::snapshot(9532)['revision']]);
 ok(C::snapshot(9532)['rows'][0]['production']==='4 Business Days','set_cost without production preserves the existing production value');
 C::rollback($noProductionApply['change_id'],['expected_revision'=>C::snapshot(9532)['revision']]);
 $add=['kind'=>'add_row','selection'=>selection('250'),'production'=>'4 Business Days','cost'=>'22'];
 $blank=$add;$blank['production']='';rejects(fn()=>C::preview(input(C::snapshot(9532)['revision'],[$blank])),'added production cannot be blank');
 $addPreview=C::preview(input(C::snapshot(9532)['revision'],[$add]));$added=C::apply($addPreview['preview_id'],['expected_revision'=>C::snapshot(9532)['revision']]);
 ok(C::snapshot(9532)['row_count']===2,'verified missing quantity row can be added');
 rejects(fn()=>C::preview(input(C::snapshot(9532)['revision'],[$add])),'duplicate row addition rejected');
 C::rollback($added['change_id'],['expected_revision'=>C::snapshot(9532)['revision']]);
 $doc=['kind'=>'add_document','url'=>'https://4over.com/media/asset/templates/cards/test.eps','label'=>'2 x 3.5 artwork template'];
 $docPreview=C::preview(input(C::snapshot(9532)['revision'],[$doc]));$docApplied=C::apply($docPreview['preview_id'],['expected_revision'=>C::snapshot(9532)['revision']]);
 ok(file_get_contents($path)===$original&&count(C::snapshot(9532)['documents'])===1,'document-only change preserves every CSV byte');
 $GLOBALS['fail_meta']=true;
 rejects(fn()=>C::rollback($docApplied['change_id'],['expected_revision'=>C::snapshot(9532)['revision']]),'failed rollback restores the applied state');
 $GLOBALS['fail_meta']=false;
 ok(C::snapshot(9532)['revision']===$docApplied['revision'],'rollback compensation retains the exact applied revision');
 C::rollback($docApplied['change_id'],['expected_revision'=>C::snapshot(9532)['revision']]);
 ok(C::snapshot(9532)['documents']===[],'document rollback restores metadata');
 $badDoc=$doc;$badDoc['url']='https://attacker.example/template.eps';
 rejects(fn()=>C::preview(input(C::snapshot(9532)['revision'],[$badDoc])),'unapproved document host rejected');
 $failure=C::preview(input(C::snapshot(9532)['revision'],[$doc]));$GLOBALS['fail_meta']=true;
 rejects(fn()=>C::apply($failure['preview_id'],['expected_revision'=>C::snapshot(9532)['revision']]),'metadata write failure causes compensated apply failure');
 $GLOBALS['fail_meta']=false;
 ok(file_get_contents($path)===$original,'failed write retains original CSV');
 $combinedInput=input(C::snapshot(9532)['revision'],[$change,$doc]);
 $combinedPreview=C::preview($combinedInput);$combined=C::apply($combinedPreview['preview_id'],['expected_revision'=>$combinedInput['expected_revision']]);
 $record=C::load('change',$combined['change_id']);
 file_put_contents($path,base64_decode($record['before_csv']));$GLOBALS['meta'][9532][C::DOC_META]=$record['before_documents'];
 ok(C::rollback($combined['change_id'],['expected_revision'=>$combined['revision']])['status']==='rolled_back','rollback crash after resources settle records restored status');
 $combinedInput=input(C::snapshot(9532)['revision'],[$change,$doc]);$combinedPreview=C::preview($combinedInput);$combined=C::apply($combinedPreview['preview_id'],['expected_revision'=>$combinedInput['expected_revision']]);
 $record=C::load('change',$combined['change_id']);$record['status']='rolling_back';C::update('change',$record['id'],$record);
 file_put_contents($path,base64_decode($record['before_csv']));
 ok(C::resource_state(9532,$record)==='mixed','interrupted rollback exact CSV/metadata mix recognized');
 C::rollback($record['id'],['expected_revision'=>$record['after_revision']]);
 ok(file_get_contents($path)===$original&&C::snapshot(9532)['documents']===[],'interrupted rollback retry accepts original applied revision only with saved rollback intent');
 $combinedInput=input(C::snapshot(9532)['revision'],[$change,$doc]);$combinedPreview=C::preview($combinedInput);$combined=C::apply($combinedPreview['preview_id'],['expected_revision'=>$combinedInput['expected_revision']]);
 $record=C::load('change',$combined['change_id']);file_put_contents($path,base64_decode($record['before_csv']));
 rejects(fn()=>C::rollback($record['id'],['expected_revision'=>$record['after_revision']]),'initial applied mixed state cannot bypass current revision guard');
 C::rollback($record['id'],['expected_revision'=>C::snapshot(9532)['revision']]);
 $combinedInput=input(C::snapshot(9532)['revision'],[$change,$doc]);$combinedPreview=C::preview($combinedInput);$combined=C::apply($combinedPreview['preview_id'],['expected_revision'=>$combinedInput['expected_revision']]);
 $record=C::load('change',$combined['change_id']);$record['status']='prepared';C::update('change',$record['id'],$record);
 $GLOBALS['meta'][9532][C::DOC_META]=$record['before_documents'];
 rejects(fn()=>C::apply($combinedPreview['preview_id'],['expected_revision'=>$combinedInput['expected_revision']]),'interrupted apply exact mixed state compensates original resources');
 $recovered=C::preview_status($combinedPreview['preview_id']);
 ok($recovered['status']==='reverted_after_failure'&&$recovered['current_revision']===$combinedInput['expected_revision']&&file_get_contents($path)===$original,'recovery receipt contains restored revision and private backup stays retained');
 $combinedInput=input(C::snapshot(9532)['revision'],[$change,$doc]);$combinedPreview=C::preview($combinedInput);$combined=C::apply($combinedPreview['preview_id'],['expected_revision'=>$combinedInput['expected_revision']]);
 $record=C::load('change',$combined['change_id']);$record['status']='prepared';C::update('change',$record['id'],$record);
 $unknown=str_replace('17.25','19.25',file_get_contents($path));file_put_contents($path,$unknown);$GLOBALS['meta'][9532][C::DOC_META]=$record['before_documents'];
 rejects(fn()=>C::apply($combinedPreview['preview_id'],['expected_revision'=>$combinedInput['expected_revision']]),'unknown interrupted state is not blindly restored');
 ok(file_get_contents($path)===$unknown,'unknown manual data preserved for review');
 file_put_contents($path,$original);$GLOBALS['meta'][9532][C::DOC_META]=[];
 $GLOBALS['admin']=false;$_SERVER['HTTP_AUTHORIZATION']='Bearer '.str_repeat('z',64);
 $GLOBALS['options'][C::CONFIG]=['enabled'=>true,'token_hash'=>hash('sha256',str_repeat('z',64)),'product_ids'=>[]];
 rejects(fn()=>C::snapshot(9532),'bearer token without product allowlist is denied');
 $GLOBALS['options'][C::CONFIG]['product_ids']=[9532];
 ok(C::snapshot(9532)['product_id']===9532,'scoped bearer token can read allowed product');
 $_SERVER['HTTP_AUTHORIZATION']='Bearer '.str_repeat('x',64);
 rejects(fn()=>C::snapshot(9532),'incorrect bearer token denied');
 $GLOBALS['admin']=true;unset($_SERVER['HTTP_AUTHORIZATION']);
 unlink($path);symlink('/etc/hosts',$path);
 rejects(fn()=>C::snapshot(9532),'symlink calculator denied');
 unlink($path);file_put_contents($path,$original.$original);
 rejects(fn()=>C::snapshot(9532),'malformed or duplicate CSV rows denied');
 C::routes();ok(count($GLOBALS['routes'])===5,'five scoped REST routes registered');
 echo "All connector tests passed.\n";
} finally {
 foreach(glob($GLOBALS['root'].'/wcdcp/*') as $p)unlink($p);
 foreach(glob($GLOBALS['root'].'/wcdcp/.*') as $p)if(is_file($p))unlink($p);
 rmdir($GLOBALS['root'].'/wcdcp');rmdir($GLOBALS['root']);
}
