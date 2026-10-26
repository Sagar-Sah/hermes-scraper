/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'Magento_Checkout/js/model/url-builder',
    'FourOver_Project/js/model/error-processor',
    'mage/storage'
], function (urlBuilder, errorProcessor, storage) {
    'use strict';

    return function saveProjectBookmark(projectBookmark) {
        projectBookmark = _.omit(projectBookmark, 'addresses_valid_ids');
        return storage.post(
            urlBuilder.createUrl('/project/bookmark/save', {}),
            JSON.stringify({projectBookmark: projectBookmark})
        ).fail(
            function (response) {
                errorProcessor.process(response);
            }
        );
    };
});
