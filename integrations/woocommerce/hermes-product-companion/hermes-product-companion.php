<?php
/**
 * Plugin Name: Hermes Product Companion Connector
 * Description: Scoped previews, audited source-cost CSV writes and rollback for the Print It custom calculator.
 * Version: 0.2.2
 * Requires PHP: 8.1
 * Requires Plugins: woocommerce
 */
defined('ABSPATH') || exit;

final class Hermes_Product_Companion {
    const VERSION = '0.2.2';
    const DOC_META = '_hermes_companion_documents';
    const CONFIG = 'hermes_companion_connection';
    const MAX_BYTES = 8388608;
    const MAX_ROWS = 100000;

    static function normalize($name) {
        $key = preg_replace('/[^a-z0-9]/', '', strtolower((string)$name));
        if (in_array($key, ['runsize','qty'], true)) return 'quantity';
        if ($key === 'turnaround') return 'production';
        return $key;
    }
    static function product_id($id) {
        if (!is_scalar($id) || !preg_match('/^[1-9][0-9]{0,11}$/D', (string)$id)) throw new InvalidArgumentException('Invalid product ID.');
        if (get_post_type((int)$id) !== 'product') throw new InvalidArgumentException('WooCommerce product not found.');
        return (int)$id;
    }
    static function external() {
        if (class_exists('Hermes_Companion_Environment_API') && Hermes_Companion_Environment_API::has_token()) return Hermes_Companion_Environment_API::credential() !== false;
        $header = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
        $config = get_option(self::CONFIG, []);
        return !empty($config['enabled']) && is_string($header) && preg_match('/^Bearer ([A-Za-z0-9_-]{40,160})$/D', $header, $match)
            && !empty($config['token_hash']) && hash_equals($config['token_hash'], hash('sha256', $match[1]));
    }
    static function permission() {
        return current_user_can('manage_woocommerce') || self::external();
    }
    static function authorize($id) {
        $id = self::product_id($id);
        if (current_user_can('manage_woocommerce') && current_user_can('edit_post', $id)) return $id;
        if (class_exists('Hermes_Companion_Environment_API') && Hermes_Companion_Environment_API::has_token()) return Hermes_Companion_Environment_API::authorize($id);
        $config = get_option(self::CONFIG, []);
        if (!self::external() || !in_array($id, $config['product_ids'] ?? [], true)) throw new RuntimeException('This product is not enabled for the connection.');
        return $id;
    }
    static function path($id) {
        $upload = wp_upload_dir();
        $base = realpath($upload['basedir']);
        $directory = $base ? realpath($base . '/wcdcp') : false;
        $path = $directory ? $directory . '/' . $id . '-printit.csv' : '';
        if (!$base || !$directory || $directory !== $base . '/wcdcp' || is_link($base . '/wcdcp')
            || !$path || is_link($path) || !is_file($path) || !is_readable($path)
            || realpath($path) !== $path) throw new RuntimeException('The supported local calculator CSV is unavailable.');
        return $path;
    }
    static function text($value) {
        if (!is_string($value) || strlen($value) > 600 || preg_match('/[\x00-\x08\x0b\x0c\x0e-\x1f]/', $value)) throw new InvalidArgumentException('Invalid option value.');
        return trim($value);
    }
    static function cost($value) {
        if (!is_string($value) || !preg_match('/^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,10})?$/D', $value)
            || (float)$value <= 0) throw new InvalidArgumentException('A positive decimal supplier cost is required.');
        return $value;
    }
    static function decimal_key($value) {
        $value=trim((string)$value);
        return str_contains($value,'.') ? rtrim(rtrim($value,'0'),'.') : $value;
    }
    static function table($raw) {
        if (!is_string($raw) || !$raw || strlen($raw) > self::MAX_BYTES || str_contains($raw, "\0")) throw new RuntimeException('Calculator CSV is empty or too large.');
        $f = fopen('php://temp', 'w+b'); fwrite($f, $raw); rewind($f);
        try {
            $headers = fgetcsv($f, 0, ',', '"', '');
            if (!is_array($headers) || count($headers) < 3 || count($headers) > 32) throw new RuntimeException('Unsupported calculator schema.');
            $normalized = [];
            foreach ($headers as $header) {
                if (!is_string($header) || strlen($header) > 100 || !($key = self::normalize($header)) || isset($normalized[$key])) throw new RuntimeException('Ambiguous calculator columns.');
                $normalized[$key] = $header;
            }
            if (!isset($normalized['quantity'], $normalized['totalprice'], $normalized['unitprice'])
                || $normalized['totalprice'] !== 'TotalPrice' || $normalized['unitprice'] !== 'UnitPrice') throw new RuntimeException('Unsupported calculator price columns.');
            $rows = []; $seen = [];
            while (($values = fgetcsv($f, 0, ',', '"', '')) !== false) {
                if ($values === [null]) continue;
                if (count($values) !== count($headers) || count($rows) >= self::MAX_ROWS) throw new RuntimeException('Invalid calculator rows.');
                $row = array_combine($headers, $values);
                $quantity = $row[$normalized['quantity']];
                if (!preg_match('/^[1-9][0-9]{0,8}$/D', trim($quantity))) throw new RuntimeException('Unsupported quantity domain.');
                self::cost(trim($row['TotalPrice']));
                foreach ($row as $value) self::text($value);
                $key = self::row_key($row, $normalized);
                if (isset($seen[$key])) throw new RuntimeException('Duplicate calculator configuration.');
                $seen[$key] = true; $rows[] = $row;
            }
            if (!$rows) throw new RuntimeException('Calculator has no rows.');
            return ['headers'=>$headers, 'columns'=>$normalized, 'rows'=>$rows];
        } finally { fclose($f); }
    }
    static function row_key($row, $columns) {
        $values = [];
        foreach ($columns as $key=>$header) if (!in_array($key, ['totalprice','unitprice'], true)) $values[$key] = trim((string)$row[$header]);
        ksort($values); return hash('sha256', wp_json_encode($values));
    }
    static function docs($id) {
        $docs = get_post_meta($id, self::DOC_META, true);
        if ($docs === '') return [];
        if (!is_array($docs) || count($docs) > 100) throw new RuntimeException('Document metadata needs review.');
        foreach ($docs as $doc) if (!is_array($doc) || !isset($doc['url'],$doc['label']) || !is_string($doc['url']) || !is_string($doc['label'])) throw new RuntimeException('Document metadata needs review.');
        return array_values($docs);
    }
    static function rule() {
        $fingerprints = [];
        if (class_exists('PILC_Repair')) {
            $file = (new ReflectionClass('PILC_Repair'))->getFileName();
            foreach ([$file, dirname($file).'/renderer.php'] as $path) if (is_file($path) && is_readable($path)) $fingerprints[basename($path)] = hash_file('sha256', $path);
        }
        return ['status'=>'unknown', 'reason'=>'effective_browser_and_cart_rule_requires_storefront_recheck',
            'fingerprint'=>hash('sha256', wp_json_encode($fingerprints)), 'files'=>$fingerprints];
    }
    static function snapshot($id) {
        $id = self::authorize($id);
        if (get_woocommerce_currency() !== 'USD') throw new RuntimeException('Only a verified USD source-cost calculator is supported.');
        $raw = file_get_contents(self::path($id)); $table = self::table($raw); $docs = self::docs($id); $rule = self::rule();
        $revision = hash('sha256', hash('sha256',$raw).'|'.wp_json_encode($docs).'|'.$rule['fingerprint']);
        $projected=[];
        foreach($table['rows'] as $row) {
            $selection=[];$production=null;
            foreach($table['columns'] as $key=>$header) {
                if(in_array($key,['unitprice','totalprice'],true))continue;
                if($key==='production')$production=trim($row[$header]);else $selection[$key]=trim($row[$header]);
            }
            $projected[]=['selection'=>$selection,'cost'=>trim($row['TotalPrice']),'production'=>$production,'raw'=>$row];
        }
        return ['schema'=>'hermes-woo-snapshot/v1','product_id'=>$id,'storefront_url'=>get_permalink($id),
            'revision'=>$revision,'csv_sha256'=>hash('sha256',$raw),'currency'=>'USD','pricing_basis'=>'supplier_cost',
            'headers'=>$table['headers'],'columns'=>$table['columns'],'rows'=>$projected,'row_count'=>count($table['rows']),
            'documents'=>$docs,'retail_rule'=>$rule,'connector_version'=>self::VERSION];
    }
    static function locked($id, $callback) {
        $path = self::path($id); $lock = dirname($path).'/.hermes-'.$id.'.lock';
        if (is_link($lock)) throw new RuntimeException('Unsafe calculator lock.');
        $f = fopen($lock, 'c');
        if (!$f || !flock($f, LOCK_EX | LOCK_NB)) { if ($f) fclose($f); throw new RuntimeException('A calculator update is already running.'); }
        try { return $callback(); } finally { flock($f, LOCK_UN); fclose($f); }
    }
    static function id($value) {
        if (!is_string($value) || !preg_match('/^[a-f0-9]{32}$/D', $value)) throw new InvalidArgumentException('Invalid change identifier.');
        return $value;
    }
    static function save($prefix, $id, $value) {
        if (!add_option('hermes_companion_'.$prefix.'_'.$id, $value, '', false)) throw new RuntimeException('Could not save the private change record.');
    }
    static function load($prefix, $id) {
        $value = get_option('hermes_companion_'.$prefix.'_'.self::id($id), null);
        if (!is_array($value)) throw new RuntimeException('Change record not found.');
        return $value;
    }
    static function update($prefix, $id, $value) {
        update_option('hermes_companion_'.$prefix.'_'.self::id($id), $value, false);
        if (get_option('hermes_companion_'.$prefix.'_'.$id) !== $value) throw new RuntimeException('Could not persist the change log.');
    }
    static function encode($headers, $rows) {
        $f = fopen('php://temp','w+b');
        try { fputcsv($f,$headers,',','"','',"\n"); foreach ($rows as $row) fputcsv($f,array_map(fn($header)=>$row[$header],$headers),',','"','',"\n"); rewind($f); return stream_get_contents($f); }
        finally { fclose($f); }
    }
    static function preview($input) {
        if (class_exists('Hermes_Companion_Environment_API')) Hermes_Companion_Environment_API::require_write_role($input['product_id']??null);
        $id = self::authorize($input['product_id'] ?? null);
        return self::locked($id, function () use ($id,$input) {
            $before = self::snapshot($id);
            if (!is_string($input['expected_revision'] ?? null) || !hash_equals($before['revision'],$input['expected_revision'])) throw new RuntimeException('Product changed. Read a new snapshot.');
            $evidence = $input['evidence'] ?? null;
            if (!is_array($evidence) || ($evidence['verified'] ?? null) !== true || !is_int($evidence['source_observed_at'] ?? null)
                || $evidence['source_observed_at'] > time()+60 || $evidence['source_observed_at'] < time()-21600
                || !preg_match('/^[a-f0-9]{64}$/D', $evidence['sha256'] ?? '')
                || !is_string($input['audit_id'] ?? null) || !preg_match('/^[a-f0-9]{32}$/D',$input['audit_id'])) throw new InvalidArgumentException('Fresh verified source evidence is required.');
            $changes = $input['changes'] ?? null;
            if (!is_array($changes) || !$changes || count($changes)>1000 || !array_is_list($changes)) throw new InvalidArgumentException('Choose one to 1000 scoped changes.');
            $raw = file_get_contents(self::path($id)); $table = self::table($raw); $rows=$table['rows']; $docs=$before['documents']; $diff=[]; $touched=[]; $csv_changed=false;
            foreach ($changes as $change) {
                if (!is_array($change) || !is_string($change['kind'] ?? null)) throw new InvalidArgumentException('Invalid change.');
                if ($change['kind']==='add_document') {
                    $url=$change['url'] ?? ''; $parts=is_string($url)?wp_parse_url($url):false;
                    if (!$parts || ($parts['scheme'] ?? '')!=='https' || !in_array(strtolower($parts['host'] ?? ''),['4over.com','www.4over.com','printitprints.com'],true)
                        || isset($parts['user']) || isset($parts['pass']) || isset($parts['port']) || strlen($url)>2000
                        || !preg_match('/\.(pdf|ai|eps|psd|zip)$/i',$parts['path'] ?? '') || isset($parts['fragment'])) throw new InvalidArgumentException('A verified HTTPS artwork document URL is required.');
                    $label=self::text($change['label'] ?? 'Artwork template');
                    if (!$label) throw new InvalidArgumentException('Document label is required.');
                    if (in_array($url,array_column($docs,'url'),true)) throw new InvalidArgumentException('Document already exists.');
                    $docs[]=['url'=>$url,'label'=>$label]; $diff[]=['kind'=>'add_document','before'=>null,'after'=>end($docs)]; continue;
                }
                if (!in_array($change['kind'],['set_cost','add_row'],true)) throw new InvalidArgumentException('Only source costs, added rows and document links can be changed.');
                $selection=$change['selection'] ?? null;
                if (!is_array($selection) || array_is_list($selection)) throw new InvalidArgumentException('Exact option selection is required.');
                $candidate=[]; $expected=[];
                foreach ($table['columns'] as $key=>$header) {
                    if (in_array($key,['unitprice','totalprice'],true)) continue;
                    if ($key==='production') {
                        if($change['kind']==='add_row'&&!array_key_exists('production',$change))throw new InvalidArgumentException('Production is required for an added row.');
                        $candidate[$header]=self::text($change['production'] ?? '');
                        if($change['kind']==='add_row'&&$candidate[$header]==='')throw new InvalidArgumentException('Production cannot be blank for an added row.');
                        continue;
                    }
                    $expected[]=$key; $candidate[$header]=self::text($selection[$key] ?? null);
                }
                $actual=array_keys($selection);sort($actual);sort($expected);
                if ($actual!==$expected || !preg_match('/^[1-9][0-9]{0,8}$/D',$candidate[$table['columns']['quantity']])) throw new InvalidArgumentException('Selection does not match the calculator schema.');
                $cost=self::cost($change['cost'] ?? null); $candidate['TotalPrice']=$cost;
                $candidate['UnitPrice']=rtrim(rtrim(number_format((float)$cost/(int)$candidate[$table['columns']['quantity']],10,'.',''),'0'),'.');
                $matches=[];
                foreach($rows as $index=>$row) {
                    $same=true;
                    foreach($table['columns'] as $field=>$header) {
                        if(in_array($field,['unitprice','totalprice'],true))continue;
                        if($field==='production'&&$change['kind']==='set_cost'&&!array_key_exists('production',$change))continue;
                        if(trim($row[$header])!==$candidate[$header]){$same=false;break;}
                    }
                    if($same)$matches[]=$index;
                }
                if($change['kind']==='set_cost'&&count($matches)===1&&isset($table['columns']['production'])&&!array_key_exists('production',$change))
                    $candidate[$table['columns']['production']]=trim($rows[$matches[0]][$table['columns']['production']]);
                $key=self::row_key($candidate,$table['columns']);
                if(isset($touched[$key]))throw new InvalidArgumentException('A configuration may be changed only once per preview.');
                $touched[$key]=true;
                if ($change['kind']==='set_cost') {
                    if (count($matches)!==1) throw new RuntimeException('The exact source-cost row is unavailable.');
                    $index=$matches[0];$old=$rows[$index];
                    if(self::decimal_key($old['TotalPrice'])===self::decimal_key($cost))continue;
                    $rows[$index]['TotalPrice']=$cost;$rows[$index]['UnitPrice']=$candidate['UnitPrice'];$csv_changed=true;
                    $diff[]=['kind'=>'set_cost','selection'=>$selection,'production'=>$change['production'] ?? null,
                        'before'=>['TotalPrice'=>$old['TotalPrice'],'UnitPrice'=>$old['UnitPrice']],
                        'after'=>['TotalPrice'=>$cost,'UnitPrice'=>$candidate['UnitPrice']]];
                } else {
                    if ($matches) throw new RuntimeException('Cannot add a duplicate configuration.');
                    $rows[]=$candidate;$csv_changed=true;$diff[]=['kind'=>'add_row','before'=>null,'after'=>$candidate];
                }
            }
            if(!$diff)throw new InvalidArgumentException('The proposed changes are already present.');
            $after_raw=$csv_changed?self::encode($table['headers'],$rows):$raw;self::table($after_raw);
            $after_revision=hash('sha256',hash('sha256',$after_raw).'|'.wp_json_encode($docs).'|'.$before['retail_rule']['fingerprint']);
            $preview_id=bin2hex(random_bytes(16));
            self::save('preview',$preview_id,['id'=>$preview_id,'product_id'=>$id,'created_at'=>time(),'expires_at'=>time()+900,
                'before_revision'=>$before['revision'],'after_revision'=>$after_revision,'after_csv'=>base64_encode($after_raw),
                'after_documents'=>$docs,'diff'=>$diff,'audit_id'=>$input['audit_id'],'evidence'=>$evidence,'change_id'=>null,
                'release_approval'=>class_exists('Hermes_Companion_Environment_API')?Hermes_Companion_Environment_API::release_context():null]);
            return ['preview_id'=>$preview_id,'product_id'=>$id,'expected_revision'=>$before['revision'],'before_revision'=>$before['revision'],'after_revision'=>$after_revision,
                'expires_at'=>time()+900,'diff'=>$diff,'requires_apply'=>true,'retail_rule_changed'=>false];
        });
    }
    static function atomic_csv($id,$raw) {
        self::table($raw);$path=self::path($id);$temp=dirname($path).'/.hermes-'.bin2hex(random_bytes(16)).'.tmp';
        $f=fopen($temp,'xb');if(!$f)throw new RuntimeException('Unable to prepare CSV write.');
        try {
            try { if(fwrite($f,$raw)!==strlen($raw)||!fflush($f))throw new RuntimeException('CSV write failed.');if(function_exists('fsync'))fsync($f); }
            finally { fclose($f); }
            chmod($temp,fileperms($path)&0777);if(!rename($temp,$path))throw new RuntimeException('Atomic CSV replacement failed.');
        } finally { if(is_file($temp))unlink($temp); }
    }
    static function write_state($id,$raw,$docs) {
        self::atomic_csv($id,$raw);
        update_post_meta($id,self::DOC_META,$docs);
        if(self::docs($id)!==$docs)throw new RuntimeException('Document metadata write failed.');
        clean_post_cache($id);if(function_exists('wc_delete_product_transients'))wc_delete_product_transients($id);
        do_action('hermes_companion_product_changed',$id);
    }
    static function change_result($change) {
        $result=array_intersect_key($change,array_flip(['preview_id','product_id','status','before_revision','after_revision','created_at','rolled_back_at','diff','audit_id','storefront_url']));
        $result['change_id']=$change['id'];$result['revision']=in_array($change['status'],['rolled_back','reverted_after_failure'],true)?$change['before_revision']:$change['after_revision'];
        $result['storefront_recheck_required']=true;
        return $result;
    }
    static function preview_status($preview_id) {
        $preview=self::load('preview',$preview_id);self::authorize($preview['product_id']);
        $change=$preview['change_id']?self::load('change',$preview['change_id']):null;
        try{$current=self::snapshot($preview['product_id']);$revision=$current['revision'];}
        catch(Throwable $error){$revision=null;}
        return ['preview_id'=>$preview['id'],'product_id'=>$preview['product_id'],
            'status'=>$change?$change['status']:'preview','expires_at'=>$preview['expires_at'],
            'current_revision'=>$revision,
            'change'=>$change?self::change_result($change):null];
    }
    static function resource_state($id,$change) {
        // A crash may leave CSV and metadata at different, precisely known
        // endpoints. Recover only those endpoints, never an unexplained edit.
        if(!isset($change['rule_fingerprint'])||self::rule()['fingerprint']!==$change['rule_fingerprint'])return 'unknown';
        $raw=file_get_contents(self::path($id));$docs=self::docs($id);
        $before=base64_decode($change['before_csv'],true);$after=base64_decode($change['after_csv'],true);
        if($before===false||$after===false)throw new RuntimeException('Invalid private recovery backup.');
        $csv_before=hash_equals(hash('sha256',$before),hash('sha256',$raw));
        $csv_after=hash_equals(hash('sha256',$after),hash('sha256',$raw));
        $docs_before=$docs===$change['before_documents'];$docs_after=$docs===$change['after_documents'];
        if($csv_before&&$docs_before)return 'before';
        if($csv_after&&$docs_after)return 'after';
        if(($csv_before||$csv_after)&&($docs_before||$docs_after))return 'mixed';
        return 'unknown';
    }
    static function apply($preview_id,$input) {
        if (class_exists('Hermes_Companion_Environment_API')) Hermes_Companion_Environment_API::require_write_role();
        $preview=self::load('preview',$preview_id);$id=self::authorize($preview['product_id']);
        if (class_exists('Hermes_Companion_Environment_API')) { Hermes_Companion_Environment_API::require_write_role($id); Hermes_Companion_Environment_API::verify_release_context($preview); }
        return self::locked($id,function()use($preview_id,$input,$id){
            $preview=self::load('preview',$preview_id);$current=self::snapshot($id);
            $resume=null;
            if($preview['change_id']) {
                $done=self::load('change',$preview['change_id']);
                if($done['status']==='prepared'&&self::resource_state($id,$done)==='mixed') {
                    try {
                        self::write_state($id,base64_decode($done['before_csv'],true),$done['before_documents']);
                        if(self::snapshot($id)['revision']!==$done['before_revision'])throw new RuntimeException('Recovery verification failed.');
                        $done['status']='reverted_after_failure';$done['recovered_at']=time();
                    } catch(Throwable $recovery_error){$done['status']='recovery_required';}
                    self::update('change',$done['id'],$done);
                    throw new RuntimeException('Interrupted apply recovered to '.$done['status'].'. Inspect saved change '.$done['id'].'.');
                }
                if(in_array($done['status'],['prepared','applied'],true)&&hash_equals($done['after_revision'],$current['revision'])) {
                    if($done['status']==='prepared'){$done['status']='applied';self::update('change',$done['id'],$done);}
                    return self::change_result($done);
                }
                if($done['status']==='prepared'&&hash_equals($done['before_revision'],$current['revision']))$resume=$done;
                else throw new RuntimeException('Saved change needs review; current product does not match its settled state. Change '.$done['id']);
            }
            if($preview['expires_at']<time())throw new RuntimeException('Preview expired.');
            if($preview['evidence']['source_observed_at']<time()-21600)throw new RuntimeException('Source evidence expired. Create a fresh preview.');
            if(($input['expected_revision']??null)!==$current['revision']||$preview['before_revision']!==$current['revision'])throw new RuntimeException('Product changed after preview.');
            $change_id=$resume?$resume['id']:bin2hex(random_bytes(16));$change=$resume?:['id'=>$change_id,'preview_id'=>$preview_id,'product_id'=>$id,'status'=>'prepared',
                'created_at'=>time(),'before_revision'=>$current['revision'],'after_revision'=>$preview['after_revision'],
                'before_csv'=>base64_encode(file_get_contents(self::path($id))),'before_documents'=>self::docs($id),
                'after_csv'=>$preview['after_csv'],'after_documents'=>$preview['after_documents'],'diff'=>$preview['diff'],
                'audit_id'=>$preview['audit_id'],'evidence'=>$preview['evidence'],'storefront_url'=>get_permalink($id),
                'rule_fingerprint'=>$current['retail_rule']['fingerprint']];
            if(!$resume)self::save('change',$change_id,$change);
            // Persist the idempotency pointer before either mutable resource.
            $preview['change_id']=$change_id;self::update('preview',$preview_id,$preview);
            try {
                self::write_state($id,base64_decode($change['after_csv'],true),$change['after_documents']);
                if(self::snapshot($id)['revision']!==$change['after_revision'])throw new RuntimeException('Post-write snapshot did not match preview.');
                $change['status']='applied';self::update('change',$change_id,$change);
            } catch(Throwable $error) {
                try { self::write_state($id,base64_decode($change['before_csv'],true),$change['before_documents']);$change['status']='reverted_after_failure'; }
                catch(Throwable $rollback_error) { $change['status']='recovery_required'; }
                self::update('change',$change_id,$change);throw new RuntimeException('Apply failed; inspect saved change '.$change_id.'.');
            }
            return self::change_result($change);
        });
    }
    static function rollback($change_id,$input) {
        if (class_exists('Hermes_Companion_Environment_API')) Hermes_Companion_Environment_API::require_write_role();
        $change=self::load('change',$change_id);$id=self::authorize($change['product_id']);
        if (class_exists('Hermes_Companion_Environment_API')) { Hermes_Companion_Environment_API::require_write_role($id); Hermes_Companion_Environment_API::verify_release_context(self::load('preview',$change['preview_id'])); }
        return self::locked($id,function()use($change_id,$input,$id){
            $change=self::load('change',$change_id);$current=self::snapshot($id);
            if($change['status']==='rolled_back'&&$current['revision']===$change['before_revision'])return self::change_result($change);
            $state=self::resource_state($id,$change);
            if(in_array($change['status'],['applied','rolling_back'],true)&&$state==='before'&&$current['revision']===$change['before_revision']) {
                $change['status']='rolled_back';$change['rolled_back_at']=time();self::update('change',$change_id,$change);return self::change_result($change);
            }
            $requested_revision=$input['expected_revision']??null;
            $revision_matches=$requested_revision===$current['revision']
                ||($change['status']==='rolling_back'&&$state==='mixed'&&$requested_revision===$change['after_revision']);
            if(!in_array($change['status'],['applied','rolling_back'],true)||!$revision_matches
                || !in_array($state,['after','mixed'],true))throw new RuntimeException('Rollback blocked because the product changed.');
            $change['status']='rolling_back';self::update('change',$change_id,$change);
            try {
                self::write_state($id,base64_decode($change['before_csv'],true),$change['before_documents']);
                if(self::snapshot($id)['revision']!==$change['before_revision'])throw new RuntimeException('Rollback verification failed.');
            } catch(Throwable $error) {
                try {
                    self::write_state($id,base64_decode($change['after_csv'],true),$change['after_documents']);
                    if(self::snapshot($id)['revision']!==$change['after_revision'])throw new RuntimeException('Rollback compensation failed.');
                    $change['status']='applied';self::update('change',$change_id,$change);
                }
                catch(Throwable $restore_error) { $change['status']='recovery_required';self::update('change',$change_id,$change); }
                throw new RuntimeException('Rollback failed; inspect saved change '.$change_id.'.');
            }
            $change['status']='rolled_back';$change['rolled_back_at']=time();self::update('change',$change_id,$change);
            return self::change_result($change);
        });
    }
    static function respond($callback) {
        try{return rest_ensure_response($callback());}
        catch(InvalidArgumentException $e){return new WP_Error('hermes_invalid',$e->getMessage(),['status'=>422]);}
        catch(Throwable $e){return new WP_Error('hermes_conflict',$e->getMessage(),['status'=>409]);}
    }
    static function routes() {
        $permission=fn()=>self::permission();
        register_rest_route('hermes-companion/v1','/snapshot/(?P<product_id>[1-9][0-9]{0,11})',['methods'=>'GET','permission_callback'=>$permission,'callback'=>fn($r)=>self::respond(fn()=>self::snapshot($r['product_id']))]);
        register_rest_route('hermes-companion/v1','/previews',['methods'=>'POST','permission_callback'=>$permission,'callback'=>fn($r)=>self::respond(fn()=>self::preview($r->get_json_params()))]);
        register_rest_route('hermes-companion/v1','/previews/(?P<id>[a-f0-9]{32})',['methods'=>'GET','permission_callback'=>$permission,'callback'=>fn($r)=>self::respond(fn()=>self::preview_status($r['id']))]);
        register_rest_route('hermes-companion/v1','/apply/(?P<id>[a-f0-9]{32})',['methods'=>'POST','permission_callback'=>$permission,'callback'=>fn($r)=>self::respond(fn()=>self::apply($r['id'],$r->get_json_params()))]);
        register_rest_route('hermes-companion/v1','/rollback/(?P<id>[a-f0-9]{32})',['methods'=>'POST','permission_callback'=>$permission,'callback'=>fn($r)=>self::respond(fn()=>self::rollback($r['id'],$r->get_json_params()))]);
    }
    static function settings() {
        if(!current_user_can('manage_woocommerce'))return;
        $notice='';
        if($_SERVER['REQUEST_METHOD']==='POST') {
            check_admin_referer('hermes_companion_settings');$config=get_option(self::CONFIG,[]);
            $ids=preg_split('/[\s,]+/',trim((string)($_POST['product_ids']??'')),-1,PREG_SPLIT_NO_EMPTY);$valid=[];
            foreach($ids as $id)if(preg_match('/^[1-9][0-9]{0,11}$/D',$id)&&get_post_type((int)$id)==='product')$valid[]=(int)$id;
            $config['product_ids']=array_values(array_unique($valid));$config['enabled']=!empty($_POST['enabled']);
            if(!empty($_POST['rotate'])) { $token=rtrim(strtr(base64_encode(random_bytes(48)),'+/','-_'),'=');$config['token_hash']=hash('sha256',$token);$notice='Copy this connection token now; it is shown once: '.$token; }
            update_option(self::CONFIG,$config,false);
        }
        $config=get_option(self::CONFIG,[]);
        echo '<div class="wrap"><h1>Hermes Product Companion</h1><p>Enable a scoped connection only for reviewed custom CSV calculator products. Preview is required before every write. Supplier costs are preserved as supplier costs.</p>';
        if($notice)echo '<div class="notice notice-success"><p>'.esc_html($notice).'</p></div>';
        echo '<form method="post">';wp_nonce_field('hermes_companion_settings');
        echo '<p><label><input type="checkbox" name="enabled" value="1" '.(!empty($config['enabled'])?'checked':'').'> Enable external connection</label></p>';
        echo '<p><label>Allowed product IDs <input name="product_ids" class="regular-text" value="'.esc_attr(implode(', ',$config['product_ids']??[])).'"></label></p>';
        echo '<p><label><input type="checkbox" name="rotate" value="1"> Generate a new connection token (invalidates the previous token)</label></p>';
        submit_button('Save connection');echo '</form></div>';
    }
    static function render_documents() {
        if(!function_exists('is_product')||!is_product())return;
        try{$docs=self::docs(get_queried_object_id());}catch(Throwable $error){return;}
        if(!$docs)return;
        echo '<section class="hermes-artwork-documents" aria-label="Artwork templates"><h3>Artwork templates</h3><ul>';
        foreach($docs as $doc)echo '<li><a href="'.esc_url($doc['url']).'" target="_blank" rel="noopener">'.esc_html($doc['label']).'</a></li>';
        echo '</ul></section>';
    }
}
add_action('rest_api_init',['Hermes_Product_Companion','routes']);
add_action('admin_menu',function(){add_management_page('Hermes Product Companion','Hermes Product Companion','manage_woocommerce','hermes-product-companion',['Hermes_Product_Companion','settings']);});
add_action('woocommerce_after_add_to_cart_form',['Hermes_Product_Companion','render_documents'],15);
require_once __DIR__.'/admin-review.php';
require_once __DIR__.'/companion-environments.php';
