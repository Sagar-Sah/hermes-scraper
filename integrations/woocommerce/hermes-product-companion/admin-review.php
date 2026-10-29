<?php
/** Browser-session review for protected staging, without exporting login state. */
defined('ABSPATH') || exit;

final class Hermes_Companion_Staging_Review {
    const HOST = 'staging3.tonyl36.sg-host.com';
    const NONCE = 'hermes_companion_staging_review';

    static function staging() {
        return strtolower((string)wp_parse_url(home_url('/'), PHP_URL_HOST)) === self::HOST;
    }
    static function allowed_ids() {
        $config = get_option(Hermes_Product_Companion::CONFIG, []);
        return array_values(array_intersect([9532, 6862], $config['product_ids'] ?? []));
    }
    static function authorize($id) {
        if (!self::staging() || !current_user_can('manage_woocommerce')) throw new RuntimeException('Protected staging administration is required.');
        $id = Hermes_Product_Companion::product_id($id);
        if (!in_array($id, self::allowed_ids(), true) || !current_user_can('edit_post', $id)) throw new RuntimeException('Enable this reviewed product in the connector allowlist first.');
        return $id;
    }
    static function summary($id) {
        $snapshot = Hermes_Product_Companion::snapshot(self::authorize($id));
        return array_intersect_key($snapshot, array_flip(['schema','product_id','storefront_url','revision','csv_sha256','currency','pricing_basis','row_count','documents','connector_version']));
    }
    static function request($input) {
        $id = self::authorize($input['product_id'] ?? null);
        $action = $input['review_action'] ?? '';
        $revision = $input['expected_revision'] ?? '';
        if (!is_string($revision)) throw new InvalidArgumentException('Read the product snapshot before previewing.');
        if ($action === 'snapshot') return ['action'=>$action, 'snapshot'=>self::summary($id)];
        if ($action === 'preview') {
            $json = $input['source_payload'] ?? '';
            if (!is_string($json) || strlen($json) > 262144) throw new InvalidArgumentException('A bounded source evidence JSON payload is required.');
            $payload = json_decode($json, true, 32, JSON_THROW_ON_ERROR);
            if (!is_array($payload) || array_is_list($payload) || array_diff(array_keys($payload), ['audit_id','evidence','changes'])) throw new InvalidArgumentException('Use only audit_id, evidence and changes in the source payload.');
            $preview = Hermes_Product_Companion::preview($payload + ['product_id'=>$id,'expected_revision'=>$revision]);
            return ['action'=>$action,'preview'=>$preview,'snapshot'=>self::summary($id)];
        }
        if ($action === 'apply') {
            $preview_id = Hermes_Product_Companion::id($input['preview_id'] ?? null);
            $saved = Hermes_Product_Companion::load('preview', $preview_id);
            if ((int)$saved['product_id'] !== $id) throw new RuntimeException('The preview belongs to another product.');
            $receipt = Hermes_Product_Companion::apply($preview_id, ['expected_revision'=>$revision]);
            return ['action'=>$action,'receipt'=>$receipt,'snapshot'=>self::summary($id)];
        }
        if ($action === 'rollback') {
            $change_id = Hermes_Product_Companion::id($input['change_id'] ?? null);
            $saved = Hermes_Product_Companion::load('change', $change_id);
            if ((int)$saved['product_id'] !== $id) throw new RuntimeException('The change belongs to another product.');
            $receipt = Hermes_Product_Companion::rollback($change_id, ['expected_revision'=>$revision]);
            return ['action'=>$action,'receipt'=>$receipt,'snapshot'=>self::summary($id)];
        }
        throw new InvalidArgumentException('Choose a review action.');
    }
    static function render() {
        if (!self::staging() || !current_user_can('manage_woocommerce')) return;
        $ids = self::allowed_ids(); $result = null; $error = '';
        $input = $_SERVER['REQUEST_METHOD'] === 'POST' ? wp_unslash($_POST) : [];
        $id = (string)($input['product_id'] ?? ($ids[0] ?? '9532'));
        $revision = is_string($input['expected_revision'] ?? null) ? $input['expected_revision'] : '';
        $payload = is_string($input['source_payload'] ?? null) ? $input['source_payload'] : '';
        $preview_id = is_string($input['preview_id'] ?? null) ? $input['preview_id'] : '';
        $change_id = is_string($input['change_id'] ?? null) ? $input['change_id'] : '';
        if ($_SERVER['REQUEST_METHOD'] === 'POST') {
            check_admin_referer(self::NONCE);
            try {
                $result = self::request($input);
                $revision = $result['snapshot']['revision'];
                if (isset($result['preview'])) $preview_id = $result['preview']['preview_id'];
                if (isset($result['receipt'])) {
                    $change_id = $result['receipt']['change_id'];
                    $preview_id = $result['receipt']['preview_id'];
                }
            } catch (Throwable $exception) { $error = $exception->getMessage(); }
        }
        echo '<div class="wrap"><h1>Hermes staging connector review</h1><p>Review and test the connector using this signed-in staging session. Allowed products: '.esc_html(implode(', ', $ids)).'. Preview protection stays enabled.</p>';
        if (!$ids) { echo '<p>Configure products 9532 and 6862 in Tools → Hermes Product Companion first.</p></div>'; return; }
        if ($error) echo '<div class="notice notice-error"><p>'.esc_html($error).'</p></div>';
        if ($result) {
            echo '<div class="notice notice-success"><p>'.esc_html(ucfirst($result['action'])).' completed. Verify the storefront separately after applying or restoring.</p></div>';
            echo '<h2>Verified server result</h2><pre id="hermes-review-result" style="max-height:28rem;overflow:auto;background:#fff;padding:1rem;white-space:pre-wrap">'.esc_html(wp_json_encode($result, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES)).'</pre>';
            echo '<p><a class="button" href="'.esc_url($result['snapshot']['storefront_url'].'?hermes_companion_review='.time()).'" target="_blank" rel="noopener">Open staging storefront for recheck</a></p>';
        }
        echo '<form method="post">'; wp_nonce_field(self::NONCE);
        echo '<p><label>Product ID <input name="product_id" type="number" min="1" value="'.esc_attr($id).'" required></label> <button class="button" name="review_action" value="snapshot">Read snapshot</button></p>';
        echo '<p><label>Expected revision<br><input name="expected_revision" class="large-text code" value="'.esc_attr($revision).'" autocomplete="off"></label></p>';
        echo '<p><label>Verified source payload JSON (audit_id, evidence, changes)<br><textarea name="source_payload" class="large-text code" rows="11" spellcheck="false">'.esc_textarea($payload).'</textarea></label></p>';
        echo '<input type="hidden" name="preview_id" value="'.esc_attr($preview_id).'"><input type="hidden" name="change_id" value="'.esc_attr($change_id).'">';
        echo '<p><button class="button button-secondary" name="review_action" value="preview">Create preview</button> ';
        if ($preview_id) echo '<button class="button button-primary" name="review_action" value="apply">Apply displayed preview</button> ';
        if ($change_id) echo '<button class="button" name="review_action" value="rollback">Restore saved original</button>';
        echo '</p></form></div>';
    }
    static function register() {
        if (self::staging()) add_management_page('Hermes staging connector review','Hermes staging connector review','manage_woocommerce','hermes-companion-staging-review',[self::class,'render']);
    }
}
add_action('admin_menu', [Hermes_Companion_Staging_Review::class, 'register']);
