<?php
/** Separate environment/role credentials and read-only catalog adapters. */
defined('ABSPATH') || exit;
final class Hermes_Companion_Environment_API {
    const CONFIG='hermes_companion_environment_connections';
    const ORIGINS=['https://printitprints.com'=>'production','https://www.printitprints.com'=>'production','https://staging3.tonyl36.sg-host.com'=>'staging'];
    static function origin() { return rtrim(function_exists('home_url')?home_url():get_option('home',''),'/'); }
    static function environment() { return self::ORIGINS[self::origin()]??'unknown'; }
    static function has_token() { return isset($_SERVER['HTTP_X_HERMES_COMPANION_TOKEN']); }
    static function credential() {
        $token=$_SERVER['HTTP_X_HERMES_COMPANION_TOKEN']??'';
        if(!is_string($token)||!preg_match('/^[A-Za-z0-9_-]{40,160}$/D',$token))return false;
        foreach(get_option(self::CONFIG,[]) as $cfg) {
            if(!is_array($cfg)||empty($cfg['enabled'])||!is_string($cfg['token_hash']??null)||!hash_equals($cfg['token_hash'],hash('sha256',$token)))continue;
            if(($cfg['site_origin']??null)!==self::origin()||($cfg['environment']??null)!==self::environment())return false;
            if(!in_array([$cfg['environment'],$cfg['role']??null],[['production','read'],['production','release'],['staging','write']],true))return false;
            return $cfg;
        }
        return false;
    }
    static function request_route() {
        // This filter can run before REST_REQUEST or WP query vars exist. Read
        // the ordinary request target, rejecting ambiguous PHP query aliases.
        $uri=$_SERVER['REQUEST_URI']??'';
        if(!is_string($uri)||strlen($uri)>8192||!str_starts_with($uri,'/')||preg_match('/[\\\\\x00-\x20\x7f]/',$uri))return null;
        $parts=parse_url($uri);
        if(!is_array($parts)||isset($parts['host'])||isset($parts['scheme'])||isset($parts['fragment']))return null;
        $path=$parts['path']??'';$query_route=null;$route_count=0;
        $query=$parts['query']??'';
        if($query!=='')foreach(explode('&',$query)as$pair){
            $bits=explode('=',$pair,2);$key=urldecode($bits[0]);
            if(str_replace(['.',' '],'_',$key)==='rest_route'&&$key!=='rest_route')return null;
            if(str_starts_with($key,'rest_route['))return null;
            if($key==='rest_route'){
                if(++$route_count!==1||!isset($bits[1]))return null;
                $query_route=urldecode($bits[1]);
            }
        }
        if(isset($_GET['rest_route'])&&(!is_string($_GET['rest_route'])||$_GET['rest_route']!==$query_route))return null;
        $prefix=function_exists('rest_get_url_prefix')?rest_get_url_prefix():'wp-json';
        if(!is_string($prefix)||!preg_match('/^[A-Za-z0-9_-]{1,80}$/D',$prefix))return null;
        $pretty=str_starts_with($path,'/'.$prefix.'/')?substr($path,strlen($prefix)+1):null;
        if($query_route!==null){
            if($pretty!==null||!in_array($path,['/','/index.php'],true))return null;
            $route=$query_route;
        }else $route=$pretty;
        if(!is_string($route)||strlen($route)>300||str_contains($route,'%')||str_contains($route,'//')||str_contains($route,'..')||preg_match('/[\\\\\x00-\x20\x7f]/',$route))return null;
        $method=$_SERVER['REQUEST_METHOD']??'';
        $patterns=['GET'=>'~^/hermes-companion/v1/(?:capabilities|catalog|inventory|environment-snapshot|(?:product-snapshot|snapshot)/[1-9][0-9]{0,11}|previews/[a-f0-9]{32})/?$~D',
            'POST'=>'~^/hermes-companion/v1/(?:previews|(?:apply|rollback)/[a-f0-9]{32})/?$~D'];
        return is_string($method)&&isset($patterns[$method])&&preg_match($patterns[$method],$route)?rtrim($route,'/'):null;
    }
    static function preview_basic_is_not_application_password($is_api_request) {
        // Do not clear authentication errors or change any REST permission.
        // SiteGround still authenticates preview Basic before PHP is reached;
        // Hermes's role token independently authorizes the connector route.
        if($is_api_request!==true||self::environment()!=='staging'||self::request_route()===null)return $is_api_request;
        $cfg=self::credential();
        if($cfg===false||$cfg['environment']!=='staging'||$cfg['role']!=='write')return $is_api_request;
        $host=$_SERVER['HTTP_HOST']??'';$expected=parse_url(self::origin(),PHP_URL_HOST);
        if(!is_string($host)||!is_string($expected)||!in_array(strtolower($host),[$expected,$expected.':443'],true)
            ||!function_exists('is_ssl')||!is_ssl())return $is_api_request;
        $headers=[];foreach(['HTTP_AUTHORIZATION','REDIRECT_HTTP_AUTHORIZATION']as$key){
            if(isset($_SERVER[$key])){if(!is_string($_SERVER[$key]))return $is_api_request;$headers[]=$_SERVER[$key];}
        }
        if(count(array_unique($headers))>1)return $is_api_request;
        $header=$headers[0]??'';
        if($header!==''){
            if(strlen($header)>8192||!preg_match('~^Basic [A-Za-z0-9+/]{4,}={0,2}$~iD',$header))return $is_api_request;
        }elseif(!isset($_SERVER['PHP_AUTH_USER'],$_SERVER['PHP_AUTH_PW'])||!is_string($_SERVER['PHP_AUTH_USER'])
            ||!is_string($_SERVER['PHP_AUTH_PW'])||$_SERVER['PHP_AUTH_USER']===''||$_SERVER['PHP_AUTH_PW']==='')return $is_api_request;
        return false;
    }
    static function permission() { return current_user_can('manage_woocommerce') || self::credential()!==false; }
    static function authorize($id) {
        $cfg=self::credential();
        if($cfg===false || (empty($cfg['all_products_read'])&&!in_array((int)$id,$cfg['product_ids']??[],true)))throw new RuntimeException('Product is not enabled for this audit connection.');
        return (int)$id;
    }
    static function require_write_role($id=null) {
        // Legacy0.1.1 connections preserve their original reviewed protocol.
        // New read credentials can never reach that protocol's write methods.
        if(!self::has_token())return;
        $cfg=self::credential();
        if($cfg===false||!in_array([$cfg['environment'],$cfg['role']],[['staging','write'],['production','release']],true))throw new RuntimeException('This connection is read-only.');
        if($id!==null&&!in_array((int)$id,$cfg['product_ids']??[],true))throw new RuntimeException('This product is not enabled for scoped writes.');
        if($id!==null&&self::adapter((int)$id)['family']!=='custom_csv')throw new RuntimeException('This calculator family has no verified write adapter.');
        if($cfg['role']==='release' && (!preg_match('/^[a-f0-9]{64}$/D',$_SERVER['HTTP_X_HERMES_RELEASE_MANIFEST']??'')||!preg_match('/^[a-f0-9]{32}$/D',$_SERVER['HTTP_X_HERMES_RELEASE_APPROVAL']??'')))throw new RuntimeException('A manually approved immutable release manifest is required.');
    }
    static function release_context() {
        if(!self::has_token()||($cfg=self::credential())===false||$cfg['role']!=='release')return null;
        return ['manifest_sha256'=>$_SERVER['HTTP_X_HERMES_RELEASE_MANIFEST']??'', 'approval_id'=>$_SERVER['HTTP_X_HERMES_RELEASE_APPROVAL']??''];
    }
    static function verify_release_context($preview) {
        $context=self::release_context();
        if($context!==null&&($preview['release_approval']??null)!==$context)throw new RuntimeException('The release approval differs from the immutable preview.');
    }
    static function product($id) {
        $id=Hermes_Product_Companion::product_id($id);
        if(!current_user_can('manage_woocommerce'))self::authorize($id);
        return $id;
    }
    static function capabilities() {
        $cfg=self::credential();
        return ['schema'=>'hermes-companion-capabilities/v1','connector_version'=>Hermes_Product_Companion::VERSION,
            'site_origin'=>self::origin(),'environment'=>self::environment(),'role'=>$cfg['role']??'admin',
            'inventory'=>true,'published_catalog'=>true,'read_families'=>['custom_csv','pila','pigf','dps','wsdisplay','woocommerce'],
            'write_families'=>['custom_csv'],'write_operations'=>['set_cost','add_row','add_document'],
            'effective_rule'=>'adapter_or_unknown','shipping'=>'adapter_or_unknown','production_release_requires_manual_manifest'=>true];
    }
    static function adapter($id) {
        $base=['family'=>'unknown','read_supported'=>false,'write_supported'=>false,'coverage_complete'=>false];
        $override=function_exists('apply_filters')?apply_filters('hermes_companion_calculator_snapshot',null,$id):null;
        if(is_array($override)&&in_array($override['family']??null,['pila','pirc','pigf','dps','wsdisplay'],true)){$override['write_supported']=false;return $override+['read_supported'=>false,'coverage_complete'=>false];}
        $candidates=[['pila','PILA_Model','profile',true],['dps','PrintIt\\DPS\\Preview','profiles',false],
                     ['pigf','PIGF_Configurator','banner_profiles',false],['wsdisplay','PrintIt\\WSDisplay\\PrivateStore','catalog',true]];
        foreach($candidates as [$family,$class,$method,$single]) {
            if(!is_callable([$class,$method]))continue;
            try {$profile=$single?$class::$method($id):($class::$method()[$id]??null);}catch(Throwable $error){continue;}
            if(!is_array($profile)||!$profile)continue;
            $safe=[];
            foreach(['fields','options','children','quantities','quantity','production','turnarounds','min','max','min_width','max_width','min_height','max_height','height_range','widths','max_quantity','maximum_quantity','minimum','minimum_per_piece','source_url','supplier','source','rate','base_per_sqft','second_side_rate','webbing_per_foot','grommet_charge','defaults','expires_at','source_contract_sha256']as$key)if(array_key_exists($key,$profile))$safe[$key]=$profile[$key];
            return ['family'=>$family,'read_supported'=>true,'write_supported'=>false,'coverage_complete'=>false,
                    'profile_sha256'=>hash('sha256',wp_json_encode($profile)),'profile'=>$safe];
        }
        // A legacy CSV may still exist under an overriding area calculator.
        // Never classify that file as the authoritative writable price source.
        foreach([['PIGF_Configurator','COROPLAST_ID'],['PIGF_Aluminum','PRODUCT_ID']]as[$class,$constant]){
            if(!class_exists($class))continue;
            $reflection=new ReflectionClass($class);
            if($reflection->getConstant($constant)===$id)return ['family'=>'pigf','read_supported'=>false,'write_supported'=>false,'coverage_complete'=>false,'reason'=>'area_runtime_adapter_required'];
        }
        if(get_post_meta($id,'_piws_source_url',true)!=='')return ['family'=>'wsdisplay','read_supported'=>false,'write_supported'=>false,'coverage_complete'=>false,'reason'=>'verified_current_wsdisplay_profile_required'];
        try {
            Hermes_Product_Companion::path($id);
            return ['family'=>'custom_csv','read_supported'=>true,'write_supported'=>true,'coverage_complete'=>false];
        } catch(Throwable $error) {}
        if(function_exists('wc_get_product')&&($p=wc_get_product($id)))$base=['family'=>'woocommerce','read_supported'=>true,'write_supported'=>false,'coverage_complete'=>false];
        return $base;
    }
    static function snapshot($id) {
        $id=self::product($id);$calculator=self::adapter($id);
        if($calculator['family']==='custom_csv')$out=Hermes_Product_Companion::snapshot($id);
        else $out=['schema'=>'hermes-woo-product-snapshot/v2','product_id'=>$id,'storefront_url'=>get_permalink($id),
                   'rows'=>[],'documents'=>Hermes_Product_Companion::docs($id),'retail_rule'=>['status'=>'unknown'],'pricing_basis'=>'unknown','currency'=>get_woocommerce_currency()];
        $out['calculator']=$calculator;$out['environment']=self::environment();$out['site_origin']=self::origin();
        $out['shipping']=function_exists('apply_filters')?apply_filters('hermes_companion_shipping_snapshot',['status'=>'unknown','reason'=>'shipping_runtime_adapter_required'],$id):['status'=>'unknown'];
        $out['artwork']=function_exists('apply_filters')?apply_filters('hermes_companion_artwork_snapshot',['status'=>'unknown','reason'=>'artwork_runtime_adapter_required'],$id):['status'=>'unknown'];
        $out['content']=['status'=>'unknown'];
        if(function_exists('wc_get_product')&&($p=wc_get_product($id))) {
            $data=$p->get_data();$safe=[];
            foreach(['name','status','slug','type','sku','price','regular_price','sale_price','description','short_description','image_id','gallery_image_ids','category_ids','shipping_class_id','weight','length','width','height','virtual','stock_status','manage_stock','stock_quantity']as$key)if(array_key_exists($key,$data))$safe[$key]=$data[$key];
            $attrs=[];foreach($p->get_attributes()as$key=>$attribute)$attrs[$key]=is_object($attribute)&&is_callable([$attribute,'get_data'])?$attribute->get_data():$attribute;
            $safe['attributes']=$attrs;$out['content']=$safe;
        }
        $out['baseline_revision']=hash('sha256',wp_json_encode($out));
        if(!isset($out['revision']))$out['revision']=$out['baseline_revision'];
        return $out;
    }
    static function inventory($page=1) {
        if(!is_numeric($page)||(int)$page<1||(int)$page>10000000)throw new InvalidArgumentException('Invalid inventory page.');
        if(!function_exists('wc_get_products'))throw new RuntimeException('WooCommerce inventory is unavailable.');
        $cfg=self::credential();
        if(!current_user_can('manage_woocommerce')&&($cfg===false||empty($cfg['all_products_read'])))throw new RuntimeException('Full catalog read permission is required.');
        $query=wc_get_products(['limit'=>100,'page'=>(int)$page,'paginate'=>true,'return'=>'ids','orderby'=>'ID','order'=>'ASC','status'=>['publish','draft','pending','private']]);
        if(!is_object($query)||!is_array($query->products??null))throw new RuntimeException('Inventory schema changed.');
        $items=[];foreach($query->products as$id){$p=wc_get_product($id);if(!$p)continue;$adapter=self::adapter((int)$id);$items[]=['product_id'=>(string)$id,'title'=>$p->get_name(),'storefront_url'=>get_permalink($id),'status'=>$p->get_status(),'calculator'=>array_intersect_key($adapter,array_flip(['family','read_supported','write_supported','coverage_complete','profile_sha256','reason']))];}
        return ['schema'=>'hermes-companion-inventory/v1','site_origin'=>self::origin(),'environment'=>self::environment(),'items'=>$items,'page'=>(int)$page,'total'=>(int)$query->total,'pages'=>(int)$query->max_num_pages,'next_cursor'=>(int)$page<(int)$query->max_num_pages?(int)$page+1:null];
    }
    static function catalog($page=1,$per_page=100) {
        if((!is_int($page)&&!is_string($page))||!preg_match('/^[1-9][0-9]{0,6}$/D',(string)$page)
            ||(!is_int($per_page)&&!is_string($per_page))||(string)$per_page!=='100')throw new InvalidArgumentException('Invalid published catalog pagination.');
        $cfg=self::credential();
        if(!current_user_can('manage_woocommerce')&&($cfg===false||empty($cfg['all_products_read'])))throw new RuntimeException('Full catalog read permission is required.');
        global $wpdb;
        if(!function_exists('wc_get_product')||!is_object($wpdb)||!is_callable([$wpdb,'prepare'])
            ||!is_callable([$wpdb,'get_var'])||!is_callable([$wpdb,'get_col'])||!is_string($wpdb->posts??null)
            ||!is_string($wpdb->prefix??null)||!preg_match('/^[A-Za-z0-9_]{0,187}$/D',$wpdb->prefix)
            ||$wpdb->posts!==$wpdb->prefix.'posts'||!preg_match('/^[A-Za-z0-9_]{1,192}$/D',$wpdb->posts))throw new RuntimeException('Published catalog authority is unavailable.');
        $page=(int)$page;
        // Core posts authority avoids Woo product-query ordering/visibility
        // filters. The sole table identifier is the validated core posts table;
        // every value (including the bounded limit/offset) is prepared. These
        // two SELECTs cannot enumerate orders, customers or other post types.
        $table='`'.$wpdb->posts.'`';
        $count_sql=$wpdb->prepare("SELECT COUNT(*) FROM $table WHERE post_type = %s AND post_status = %s",'product','publish');
        if(!is_string($count_sql))throw new RuntimeException('Published catalog query could not be prepared.');
        $count=$wpdb->get_var($count_sql);
        if(!empty($wpdb->last_error)||(!is_int($count)&&!is_string($count))||!preg_match('/^(?:0|[1-9][0-9]{0,11})$/D',(string)$count)
            ||filter_var($count,FILTER_VALIDATE_INT,['options'=>['min_range'=>0]])===false)throw new RuntimeException('Published catalog count is unavailable.');
        $total=(int)$count;$pages=max(1,(int)ceil($total/100));
        if($page>$pages)throw new RuntimeException('Published catalog page coverage is incomplete.');
        $ids_sql=$wpdb->prepare("SELECT ID FROM $table WHERE post_type = %s AND post_status = %s ORDER BY ID ASC LIMIT %d OFFSET %d",'product','publish',100,($page-1)*100);
        if(!is_string($ids_sql))throw new RuntimeException('Published catalog query could not be prepared.');
        $ids=$wpdb->get_col($ids_sql);
        if(!empty($wpdb->last_error)||!is_array($ids)||count($ids)!==max(0,min(100,$total-($page-1)*100)))throw new RuntimeException('Published catalog page coverage is incomplete.');
        $items=[];$previous=0;
        foreach($ids as$id){
            if((!is_int($id)&&!is_string($id))||!preg_match('/^[1-9][0-9]{0,11}$/D',(string)$id)||(int)$id<=$previous)throw new RuntimeException('Published catalog IDs are invalid or duplicated.');
            $id=(int)$id;$previous=$id;$product=wc_get_product($id);
            if(!$product||!is_callable([$product,'get_status'])||$product->get_status()!=='publish'
                ||!is_callable([$product,'get_name'])||(is_callable([$product,'get_id'])&&(int)$product->get_id()!==$id))throw new RuntimeException('Published product could not be read.');
            $name=$product->get_name();$url=get_permalink($id);$parsed=is_string($url)?parse_url($url):false;
            if(!is_string($name)||!is_array($parsed)||($parsed['scheme']??'')!=='https'||isset($parsed['user'])||isset($parsed['pass'])
                ||isset($parsed['query'])||isset($parsed['fragment'])||isset($parsed['port'])
                ||'https://'.($parsed['host']??'')!==self::origin()||empty($parsed['path'])||$parsed['path']==='/')throw new RuntimeException('Published product identity is incomplete.');
            if(function_exists('wp_get_post_terms'))$terms=wp_get_post_terms($id,'product_cat',['orderby'=>'term_id','order'=>'ASC']);
            elseif(function_exists('get_the_terms'))$terms=get_the_terms($id,'product_cat');
            else throw new RuntimeException('Published product categories are unavailable.');
            if($terms===false)$terms=[];
            if(!is_array($terms))throw new RuntimeException('Published product categories could not be read.');
            $categories=[];$seen=[];
            foreach($terms as$term){
                if(!is_object($term)||(!is_int($term->term_id??null)&&!is_string($term->term_id??null))
                    ||!preg_match('/^[1-9][0-9]{0,11}$/D',(string)$term->term_id)||!is_string($term->name??null)
                    ||isset($seen[(int)$term->term_id])||(isset($term->taxonomy)&&$term->taxonomy!=='product_cat'))throw new RuntimeException('Published product category identity is incomplete.');
                $seen[(int)$term->term_id]=true;$categories[]=['id'=>(int)$term->term_id,'name'=>$term->name];
            }
            usort($categories,fn($a,$b)=>$a['id']<=>$b['id']);
            $items[]=['id'=>$id,'name'=>$name,'permalink'=>$url,'categories'=>$categories];
        }
        return ['schema'=>'hermes-companion-catalog/v1','site_origin'=>self::origin(),'environment'=>self::environment(),
            'items'=>$items,'page'=>$page,'total'=>$total,'pages'=>$pages,'next_cursor'=>$page<$pages?$page+1:null];
    }
    static function tree_fingerprint($directory) {
        if(!is_string($directory)||!is_dir($directory)||is_link($directory))return ['status'=>'unknown','reason'=>'code_directory_unavailable'];
        $rows=[];$bytes=0;$complete=true;
        try {
            $walk=new RecursiveIteratorIterator(new RecursiveDirectoryIterator($directory,FilesystemIterator::SKIP_DOTS));
            foreach($walk as$file){
                if($file->isLink()){$complete=false;continue;}
                if(!$file->isFile()||!in_array(strtolower($file->getExtension()),['php','js','css','json','html','svg'],true))continue;
                if(count($rows)>=10000||$file->getSize()>33554432){$complete=false;continue;}
                $relative=substr($file->getPathname(),strlen(rtrim($directory,'/'))+1);$hash=hash_file('sha256',$file->getPathname());
                if($hash===false){$complete=false;continue;}$rows[$relative]=$hash;$bytes+=$file->getSize();
            }
        } catch(Throwable $error){$complete=false;}
        ksort($rows);return ['status'=>$complete?'match':'unknown','sha256'=>hash('sha256',wp_json_encode($rows)),'files'=>count($rows),'bytes'=>$bytes,'complete'=>$complete];
    }
    static function environment_snapshot() {
        if(!self::permission())throw new RuntimeException('Audit connection required.');
        if(!function_exists('get_plugins')&&is_file(ABSPATH.'wp-admin/includes/plugin.php'))require_once ABSPATH.'wp-admin/includes/plugin.php';
        $plugins=[];$active=get_option('active_plugins',[]);
        if(function_exists('get_plugins'))foreach(get_plugins()as$path=>$info){
            $row=['version'=>(string)($info['Version']??''),'active'=>in_array($path,$active,true)];
            if($row['active']&&defined('WP_PLUGIN_DIR')){
                $directory=dirname($path);$base=realpath(WP_PLUGIN_DIR);$target=realpath(WP_PLUGIN_DIR.'/'.$directory);
                $row['code']=($base&&$target&&($target===$base||str_starts_with($target,$base.'/')))?($directory==='.'?['status'=>'match','sha256'=>hash_file('sha256',WP_PLUGIN_DIR.'/'.$path),'complete'=>true]:self::tree_fingerprint($target)):['status'=>'unknown'];
            }
            $plugins[$path]=$row;
        }
        $themes=[];
        if(function_exists('wp_get_theme')){
            $theme=wp_get_theme();$themes[$theme->get_stylesheet()]=['version'=>$theme->get('Version'),'code'=>self::tree_fingerprint($theme->get_stylesheet_directory())];
            if($theme->parent()){$parent=$theme->parent();$themes[$parent->get_stylesheet()]=['version'=>$parent->get('Version'),'code'=>self::tree_fingerprint($parent->get_stylesheet_directory())];}
        }
        $mu=defined('WPMU_PLUGIN_DIR')?self::tree_fingerprint(WPMU_PLUGIN_DIR):['status'=>'unknown'];
        $css=function_exists('wp_get_custom_css')?['status'=>'match','sha256'=>hash('sha256',wp_get_custom_css())]:['status'=>'unknown'];
        $snippets=['status'=>'unknown','reason'=>'snippet_inventory_unavailable'];
        if(function_exists('get_posts')){
            $found=get_posts(['post_type'=>'wpcode','post_status'=>['publish','draft','private'],'numberposts'=>1001,'orderby'=>'ID','order'=>'ASC']);$rows=[];
            if(is_array($found)&&count($found)<=1000){foreach($found as$p)$rows[(string)$p->ID]=hash('sha256',(string)$p->post_status.'|'.(string)$p->post_content);$snippets=['status'=>'match','count'=>count($rows),'sha256'=>hash('sha256',wp_json_encode($rows))];}
        }
        $upload=wp_upload_dir();$csvs=[];$dir=$upload['basedir'].'/wcdcp';$complete=true;
        if(is_dir($dir)&&!is_link($dir))foreach(glob($dir.'/*-printit.csv')as$path){if(count($csvs)>=10000||is_link($path)||!is_file($path)){$complete=false;continue;}if(preg_match('/^[1-9][0-9]{0,11}-printit\.csv$/D',basename($path)))$csvs[basename($path)]=hash_file('sha256',$path);}
        ksort($csvs);
        $isolation=['search_indexing'=>get_option('blog_public','1')==='0'?'disabled':'enabled','outbound_mail'=>'unknown','payments'=>'unknown','scheduled_actions'=>'unknown','outbound_http'=>'unknown'];
        if(function_exists('apply_filters'))$isolation=apply_filters('hermes_companion_staging_isolation_snapshot',$isolation);
        $runtime=function_exists('apply_filters')?apply_filters('hermes_companion_runtime_fingerprints',['retail'=>'unknown','shipping'=>'unknown']):['retail'=>'unknown','shipping'=>'unknown'];
        $out=['schema'=>'hermes-companion-environment-snapshot/v1','site_origin'=>self::origin(),'environment'=>self::environment(),'connector_version'=>Hermes_Product_Companion::VERSION,'plugins'=>$plugins,'themes'=>$themes,'mu_plugins'=>$mu,'custom_css'=>$css,'snippets'=>$snippets,'custom_calculator_files'=>['status'=>$complete?'match':'unknown','files'=>$csvs,'sha256'=>hash('sha256',wp_json_encode($csvs))],'staging_isolation'=>$isolation,'runtime_fingerprints'=>$runtime];
        $out['snapshot_sha256']=hash('sha256',wp_json_encode($out));return $out;
    }
    static function routes() {
        $p=fn()=>self::permission();$r=fn($fn)=>Hermes_Product_Companion::respond($fn);
        register_rest_route('hermes-companion/v1','/capabilities',['methods'=>'GET','permission_callback'=>$p,'callback'=>fn()=>$r(fn()=>self::capabilities())]);
        register_rest_route('hermes-companion/v1','/inventory',['methods'=>'GET','permission_callback'=>$p,'callback'=>fn($q)=>$r(fn()=>self::inventory($q['page']??1))]);
        register_rest_route('hermes-companion/v1','/catalog',['methods'=>'GET','permission_callback'=>$p,'callback'=>fn($q)=>$r(fn()=>self::catalog($q['page']??1,$q['per_page']??100))]);
        register_rest_route('hermes-companion/v1','/environment-snapshot',['methods'=>'GET','permission_callback'=>$p,'callback'=>fn()=>$r(fn()=>self::environment_snapshot())]);
        register_rest_route('hermes-companion/v1','/product-snapshot/(?P<product_id>[1-9][0-9]{0,11})',['methods'=>'GET','permission_callback'=>$p,'callback'=>fn($q)=>$r(fn()=>self::snapshot($q['product_id']))]);
    }
    static function settings() {
        if(!current_user_can('manage_woocommerce'))return;
        $notice='';$origin=self::origin();$environment=self::environment();
        if(($_SERVER['REQUEST_METHOD']??'')==='POST'){
            check_admin_referer('hermes_companion_environment_settings');
            $role=(string)($_POST['connection_role']??'read');
            if(!in_array([$environment,$role],[['production','read'],['production','release'],['staging','write']],true))wp_die('The connection role does not match this site.');
            $ids=preg_split('/[\s,]+/',trim((string)($_POST['product_ids']??'')),-1,PREG_SPLIT_NO_EMPTY);$valid=[];
            foreach($ids as$id)if(preg_match('/^[1-9][0-9]{0,11}$/D',$id)&&get_post_type((int)$id)==='product')$valid[]=(int)$id;
            $cfg=get_option(self::CONFIG,[]);$token=rtrim(strtr(base64_encode(random_bytes(48)),'+/','-_'),'=');
            $cfg[$role]=['enabled'=>true,'environment'=>$environment,'role'=>$role,'site_origin'=>$origin,'all_products_read'=>true,'product_ids'=>array_values(array_unique($valid)),'token_hash'=>hash('sha256',$token)];
            update_option(self::CONFIG,$cfg,false);$notice='Copy this role-specific token now; shown once: '.$token;
        }
        echo '<div class="wrap"><h1>Hermes audit connections</h1><p>Production audit tokens are read-only. Staging writes and manual production releases use separate credentials. Only custom CSV repairs are supported; unsupported families remain held. Existing preview protection stays enabled.</p>';
        if($notice)echo '<p>'.esc_html($notice).'</p>';
        echo '<form method="post">';wp_nonce_field('hermes_companion_environment_settings');
        echo '<label>Role <select name="connection_role">';foreach(($environment==='production'?['read','release']:['write'])as$role)echo '<option value="'.esc_attr($role).'">'.esc_html($role).'</option>';echo '</select></label>';
        echo '<p><label>Product IDs allowed for writes <input name="product_ids" class="regular-text"></label></p><p>All products may be read; writes remain restricted to these IDs. A release credential never grants the AI director production publishing permission.</p>';
        submit_button('Generate scoped connection token');echo '</form></div>';
    }
}
if(function_exists('add_filter'))add_filter('application_password_is_api_request',['Hermes_Companion_Environment_API','preview_basic_is_not_application_password'],20,1);
add_action('rest_api_init',['Hermes_Companion_Environment_API','routes']);
add_action('admin_menu',function(){add_management_page('Hermes audit connections','Hermes audit connections','manage_woocommerce','hermes-companion-environments',['Hermes_Companion_Environment_API','settings']);});
