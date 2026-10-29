<?php
// Bounded synthetic published catalog; no WordPress, source data or network.
define('ABSPATH',__DIR__.'/');
$GLOBALS['origin']='https://printitprints.com';$GLOBALS['options']=[];$GLOBALS['admin']=false;$GLOBALS['calls']=[];
$GLOBALS['missing']=null;$GLOBALS['status']='publish';$GLOBALS['wrong_id']=false;$GLOBALS['bad_url']=false;$GLOBALS['terms_mode']='normal';
function home_url(){return $GLOBALS['origin'];}
function get_option($key,$default=false){return $GLOBALS['options'][$key]??$default;}
function current_user_can(...$args){return $GLOBALS['admin'];}
function add_action(...$args){}
function add_filter(...$args){}
function wc_get_products($query){throw new RuntimeException('Filtered Woo product inventory must not be used by published catalog');}
class CatalogDatabaseFixture {
 public $prefix='wp_';public $posts='wp_posts';public $last_error='';
 function prepare($sql,...$args){
  $GLOBALS['calls'][]=['prepare',$sql,$args];$index=0;
  return preg_replace_callback('/%[sd]/',function($match)use($args,&$index){$value=$args[$index++];return $match[0]==='%d'?(string)(int)$value:"'".str_replace("'","''",$value)."'";},$sql);
 }
 function get_var($sql){$GLOBALS['calls'][]=['get_var',$sql];return $GLOBALS['query']->total;}
 function get_col($sql){$GLOBALS['calls'][]=['get_col',$sql];return $GLOBALS['query']->products;}
}
$GLOBALS['wpdb']=new CatalogDatabaseFixture();
function get_permalink($id){return $GLOBALS['bad_url']?'https://unrelated.example.org/shop/item/':$GLOBALS['origin'].'/shop/item-'.$id.'/';}
function wc_get_product($id){
 if($GLOBALS['missing']===$id)return false;
 return new class($id){
  function __construct(private $id){}
  function get_id(){return $GLOBALS['wrong_id']?$this->id+1:$this->id;}
  function get_status(){return $GLOBALS['status'];}
  function get_name(){return 'Synthetic published product '.$this->id;}
  function get_price(){throw new RuntimeException('Price must not be loaded by catalog');}
  function get_data(){throw new RuntimeException('Product data and costs must not be loaded by catalog');}
 };
}
function wp_get_post_terms($id,$taxonomy,$args){
 if($taxonomy!=='product_cat')throw new RuntimeException('Wrong taxonomy');
 return match($GLOBALS['terms_mode']){
  'none'=>false,'error'=>(object)['error'=>'synthetic_taxonomy_error'],
  'duplicate'=>[(object)['term_id'=>9,'name'=>'A'],(object)['term_id'=>9,'name'=>'B']],
  'invalid'=>[(object)['term_id'=>0,'name'=>'Invalid']],
  default=>[(object)['term_id'=>15,'name'=>'Postcards','taxonomy'=>'product_cat'],(object)['term_id'=>9,'name'=>'Standard Print','taxonomy'=>'product_cat']]
 };
}
class PILA_Model {static function profile($id){throw new RuntimeException('Calculator profiles must not be loaded by catalog');}}
require dirname(__DIR__).'/hermes-product-companion.php';
use Hermes_Product_Companion as C;
use Hermes_Companion_Environment_API as E;
function ok($value,$label){if(!$value)throw new RuntimeException('FAIL '.$label);echo 'PASS '.$label."\n";}
function rejects($fn,$label){try{$fn();}catch(Throwable $error){echo 'PASS '.$label."\n";return;}throw new RuntimeException('FAIL accepted '.$label);}
function query($ids,$total,$pages){$GLOBALS['query']=(object)['products'=>$ids,'total'=>$total,'max_num_pages'=>$pages];}
function token($all=true){$value=str_repeat('fixture_',8);$_SERVER['HTTP_X_HERMES_COMPANION_TOKEN']=$value;
 $GLOBALS['options'][E::CONFIG]=['read'=>['enabled'=>true,'site_origin'=>$GLOBALS['origin'],'environment'=>'production','role'=>'read',
  'token_hash'=>hash('sha256',$value),'all_products_read'=>$all,'product_ids'=>[101]]];}
token();query(range(101,200),201,3);
$first=E::catalog();
ok($first['schema']==='hermes-companion-catalog/v1'&&$first['site_origin']===$GLOBALS['origin']&&$first['environment']==='production','catalog identifies exact published-site contract');
ok(count($first['items'])===100&&$first['page']===1&&$first['total']===201&&$first['pages']===3&&$first['next_cursor']===2,'full published page preserves total and next cursor');
ok($GLOBALS['calls'][0]===['prepare','SELECT COUNT(*) FROM `wp_posts` WHERE post_type = %s AND post_status = %s',['product','publish']],'count is a prepared readonly core-posts query restricted to published products');
ok($GLOBALS['calls'][2]===['prepare','SELECT ID FROM `wp_posts` WHERE post_type = %s AND post_status = %s ORDER BY ID ASC LIMIT %d OFFSET %d',['product','publish',100,0]],'core ID query fixes ascending order and prepared100-row limit independently of Woo filters');
ok($GLOBALS['calls'][3]===['get_col',"SELECT ID FROM `wp_posts` WHERE post_type = 'product' AND post_status = 'publish' ORDER BY ID ASC LIMIT 100 OFFSET 0"],'only readonly published product IDs are executed');
ok(array_keys($first['items'][0])===['id','name','permalink','categories']&&$first['items'][0]['id']===101,'catalog does not load or return calculator data or prices');
ok($first['items'][0]['categories']===[['id'=>9,'name'=>'Standard Print'],['id'=>15,'name'=>'Postcards']],'public product categories use stable safe IDs and names');
ok(E::capabilities()['published_catalog']===true&&C::VERSION==='0.2.2','capability and connector build advertise published catalog');
query(range(201,300),201,3);$second=E::catalog('2','100');ok(count($second['items'])===100&&$second['next_cursor']===3,'query-string integer pagination remains supported');
ok(end($GLOBALS['calls'])[1]==="SELECT ID FROM `wp_posts` WHERE post_type = 'product' AND post_status = 'publish' ORDER BY ID ASC LIMIT 100 OFFSET 100",'second page uses a prepared100-row offset');
query([301],201,3);$last=E::catalog(3);ok(count($last['items'])===1&&$last['next_cursor']===null,'final partial page has exact cardinality and no next cursor');
ok(str_ends_with(end($GLOBALS['calls'])[1],'LIMIT 100 OFFSET 200'),'final page uses its exact prepared offset');
query([],0,0);$empty=E::catalog();ok($empty['items']===[]&&$empty['pages']===1&&$empty['next_cursor']===null,'empty published catalog has one terminal empty page');
foreach([0,-1,1.5,true,'01','1.5','1e2','2suffix',[]]as$page)rejects(fn()=>E::catalog($page),'noncanonical page value is rejected');
foreach([99,101,100.0,true,'0100','100suffix',[]]as$limit)rejects(fn()=>E::catalog(1,$limit),'per-page limit cannot change from100');
query([],0,0);rejects(fn()=>E::catalog(2),'out-of-range empty page cannot fabricate a later cursor');
query(range(101,199),201,3);rejects(fn()=>E::catalog(),'short nonterminal query page is not silently treated as complete');
foreach([null,false,-1,1.5,'01','1e3','201suffix']as$total){query([], $total,0);rejects(fn()=>E::catalog(),'invalid authoritative count is rejected');}
query([101],'1',1);ok(E::catalog()['total']===1,'native WordPress numeric-string count is supported');
foreach(['wp_posts; DROP TABLE customers','wp.posts','`wp_posts`','wp_posts--','wp_users',[]]as$table){$before=count($GLOBALS['calls']);$GLOBALS['wpdb']->posts=$table;rejects(fn()=>E::catalog(),'unsafe or noncore posts table identifier is rejected');ok(count($GLOBALS['calls'])===$before,'invalid table identifier sends no SQL');}
$GLOBALS['wpdb']->posts='wp_posts';$GLOBALS['wpdb']->last_error='synthetic database failure';rejects(fn()=>E::catalog(),'database failures cannot certify an empty inventory');$GLOBALS['wpdb']->last_error='';
query([101,101],2,1);rejects(fn()=>E::catalog(),'duplicate product IDs cannot appear as distinct inventory');
query([102,101],2,1);rejects(fn()=>E::catalog(),'out-of-order product IDs require review');
query([101],1,1);$GLOBALS['missing']=101;rejects(fn()=>E::catalog(),'missing product is not skipped');$GLOBALS['missing']=null;
$GLOBALS['status']='draft';rejects(fn()=>E::catalog(),'nonpublished product cannot enter published catalog');$GLOBALS['status']='publish';
$GLOBALS['wrong_id']=true;rejects(fn()=>E::catalog(),'returned product object must retain its queried ID');$GLOBALS['wrong_id']=false;
$GLOBALS['bad_url']=true;rejects(fn()=>E::catalog(),'external permalink cannot impersonate this store product');$GLOBALS['bad_url']=false;
foreach(['error','duplicate','invalid']as$mode){$GLOBALS['terms_mode']=$mode;rejects(fn()=>E::catalog(),'missing or invalid category evidence is not skipped');}
$GLOBALS['terms_mode']='none';ok(E::catalog()['items'][0]['categories']===[],'published product with no categories is explicitly represented');$GLOBALS['terms_mode']='normal';
token(false);$before=count($GLOBALS['calls']);rejects(fn()=>E::catalog(),'product-scoped token does not grant all-catalog read');ok(count($GLOBALS['calls'])===$before,'failed catalog authorization sends no Woo query');
token();$_SERVER['HTTP_X_HERMES_COMPANION_TOKEN']=str_repeat('bad_',16);rejects(fn()=>E::catalog(),'invalid token is not accepted for catalog reads');
unset($_SERVER['HTTP_X_HERMES_COMPANION_TOKEN']);$GLOBALS['admin']=true;ok(count(E::catalog()['items'])===1,'ordinary Woo administrator can inspect catalog without enlarging external token scope');
echo "All published catalog tests passed.\n";
