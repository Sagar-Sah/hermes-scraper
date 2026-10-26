/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'jquery',
    'FourOver_Project/js/model/error-processor'
], function ($, errorProcessor) {
    'use strict';

    return function postRequest(url, payload) {
        return $.ajax({
            url: url,
            data: payload,
            type: 'post'
        }).fail(
            function (response) {
                errorProcessor.process(response, payload);
            }
        );
    };
});
