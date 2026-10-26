/**
 * Created by QASource.
 * Project: 4over
 */
define([
    'jquery',
    'Magento_Ui/js/modal/modal',
    'require',
    'Magento_Customer/js/customer-data',
    'FourOver_B2COnboarding/js/model/b2c-onboarding-config',
    'mage/validation',
    'mage/cookies'
], function ($, modal, req, customerData, b2cOnboardingConfig) {
    'use strict';

    let pendingDeferred = null,
        modalReady = false,
        needCheckRequest = null,
        businessTypesPopulated = false,
        OTHER_TYPE = 'other';

    function b2cCfg() {
        return b2cOnboardingConfig.get();
    }

    function b2cPrivateData() {
        return customerData.get('b2c_onboarding')() || {};
    }

    function formKey() {
        return $.mage.cookies.get('form_key') || '';
    }

    function ensureValidationMethod() {
        if ($.validator && !$.validator.methods.validate) {
            $.validator.addMethod('validate', function () {
                return true;
            });
        }
    }

    function populateCustomerName($root) {
        let customer = customerData.get('customer')() || {},
            firstname = customer.firstname || '',
            lastname = customer.fullname ? customer.fullname.replace(firstname, '').trim() : '';

        $root.find('#b2c-b-firstname, #b2c-s-firstname').val(firstname);
        $root.find('#b2c-b-lastname, #b2c-s-lastname').val(lastname);
    }

    function showModalMessage(message) {
        let $message = $('#b2c-company-modal-message');

        $message.find('[data-role="b2c-company-modal-message-text"]').text(message);
        $message.show();
    }

    function hideModalMessage() {
        $('#b2c-company-modal-message').hide()
            .find('[data-role="b2c-company-modal-message-text"]')
            .text('');
    }

    function loadCountries($countrySelect, baseUrl) {
        $countrySelect.empty().append(
            $('<option>').val('').text($.mage.__('Select country'))
        );

        return $.getJSON(baseUrl + 'rest/V1/directory/countries').done(function (countries) {
            $.each(countries || [], function (i, country) {
                if (!country.two_letter_abbreviation || !country.full_name_english) {
                    return;
                }

                $countrySelect.append(
                    $('<option>')
                        .val(country.two_letter_abbreviation)
                        .text(country.full_name_english)
                );
            });

            if ($countrySelect.find('option[value="US"]').length) {
                $countrySelect.val('US');
            }
        }).fail(function () {
            $countrySelect.empty().append(
                $('<option>').val('US').text('United States')
            );

            $countrySelect.val('US');
        });
    }

    function loadRegionsByCountry($regionSelect, countryId, baseUrl) {
        $regionSelect.empty().append(
            $('<option>').val('').text($.mage.__('Select state'))
        );

        if (!countryId) {
            return $.Deferred().resolve().promise();
        }

        return $.getJSON(baseUrl + 'rest/V1/directory/countries/' + countryId).done(function (data) {
            let regions = data.available_regions || [];

            if (!regions.length) {
                $regionSelect.append(
                    $('<option>').val('0').text($.mage.__('N/A'))
                );

                $regionSelect.val('0');

                return;
            }

            $.each(regions, function (i, region) {
                $regionSelect.append(
                    $('<option>').val(region.id).text(region.name)
                );
            });
        }).fail(function () {
            if (countryId === 'US') {
                $regionSelect.empty().append(
                    $('<option>').val('').text($.mage.__('Select state'))
                );
            } else {
                $regionSelect.empty().append(
                    $('<option>').val('0').text($.mage.__('N/A'))
                );

                $regionSelect.val('0');
            }
        });
    }

    function populateBusinessTypes($root) {
        let cfg = b2cCfg(),
            $select = $root.find('#b2c-modal-business-type'),
            options = cfg.business_type_options || [];

        if (businessTypesPopulated) {
            return;
        }

        $select.find('option:not([value=""])').remove();

        options.forEach(function (row) {
            if (!row.value) {
                return;
            }

            $select.append(
                $('<option>').val(row.value).text(row.label)
            );
        });

        businessTypesPopulated = true;
    }

    function normalizeValidateAttribute($element) {
        let rawValidate = $element.attr('data-validate'),
            rules;

        if (!rawValidate) {
            $element.removeData('validate');
            return;
        }

        rules = JSON.parse(rawValidate);
        delete rules.validate;

        if ($.isEmptyObject(rules)) {
            $element.removeAttr('data-validate');
            $element.removeData('validate');
            return;
        }

        $element.attr('data-validate', JSON.stringify(rules));
        $element.removeData('validate');
    }

    function normalizeFormValidateAttributes($form) {
        $form.find('[data-validate]').each(function () {
            normalizeValidateAttribute($(this));
        });
    }

    function removeValidationRule($form, fieldName, ruleName) {
        let validator = $form.data('validator');

        if (!validator || !validator.settings || !validator.settings.rules || !validator.settings.rules[fieldName]) {
            return;
        }

        delete validator.settings.rules[fieldName][ruleName];
    }

    function setRequiredRule($form, fieldName, isRequired) {
        let validator = $form.data('validator');

        if (!validator || !validator.settings || !validator.settings.rules) {
            return;
        }

        if (!validator.settings.rules[fieldName]) {
            validator.settings.rules[fieldName] = {};
        }

        delete validator.settings.rules[fieldName].validate;

        if (isRequired) {
            validator.settings.rules[fieldName].required = true;
            return;
        }

        delete validator.settings.rules[fieldName].required;
    }

    function cleanValidationRules($form) {
        let validator = $form.data('validator');

        normalizeFormValidateAttributes($form);

        if (!validator || !validator.settings || !validator.settings.rules) {
            return;
        }

        $.each(validator.settings.rules, function (fieldName, rules) {
            if (!rules) {
                return;
            }

            delete rules.validate;

            if ($.isEmptyObject(rules)) {
                delete validator.settings.rules[fieldName];
            }
        });
    }

    function initFormValidation($form) {
        if (!$form.length) {
            return;
        }

        ensureValidationMethod();
        normalizeFormValidateAttributes($form);

        if (!$form.data('mageValidation')) {
            $form.validation({
                ignore: ':hidden, .b2c-cm-hidden, .b2c-cm-hidden *'
            });
        }

        cleanValidationRules($form);
        setRequiredRule($form, 'business_type', true);
    }

    function toggleOtherField() {
        let value = ($('#b2c-modal-business-type').val() || '').toString(),
            $wrap = $('#b2c-modal-other-business-type-wrap'),
            $input = $('#b2c-modal-other-business-type'),
            $form = $('#b2c-company-modal-form');

        if (value === OTHER_TYPE) {
            $wrap.removeClass('b2c-cm-hidden').show();

            $input
                .addClass('required-entry')
                .attr('data-validate', '{"required":true}')
                .removeData('validate');

            setRequiredRule($form, 'other_business_type', true);
            removeValidationRule($form, 'other_business_type', 'validate');

            return;
        }

        setRequiredRule($form, 'other_business_type', false);
        removeValidationRule($form, 'other_business_type', 'validate');

        $wrap.addClass('b2c-cm-hidden').hide();

        $input
            .val('')
            .removeClass('required-entry mage-error')
            .removeAttr('data-validate')
            .removeData('validate')
            .removeAttr('aria-invalid')
            .removeAttr('aria-describedby');

        $input.closest('.field').find('label.mage-error, div.mage-error').remove();
    }

    function collectAddress(prefix) {
        let $region = $('#b2c-' + prefix + '-region'),
            regionId = parseInt($region.val(), 10),
            regionText = $region.find('option:selected').text() || '';

        if (regionText === $.mage.__('Select state') || regionText === $.mage.__('N/A')) {
            regionText = '';
        }

        return {
            firstname: ($('#b2c-' + prefix + '-firstname').val() || '').trim(),
            lastname: ($('#b2c-' + prefix + '-lastname').val() || '').trim(),
            street: [
                ($('#b2c-' + prefix + '-street').val() || '').trim(),
                ($('#b2c-' + prefix + '-street2').val() || '').trim()
            ],
            city: ($('#b2c-' + prefix + '-city').val() || '').trim(),
            region: regionText,
            region_id: regionId > 0 ? regionId : null,
            postcode: ($('#b2c-' + prefix + '-postcode').val() || '').trim(),
            country_id: ($('#b2c-' + prefix + '-country').val() || 'US').trim(),
            telephone: ($('#b2c-' + prefix + '-phone').val() || '').trim()
        };
    }

    function validateModal() {
        let $form = $('#b2c-company-modal-form');

        hideModalMessage();
        initFormValidation($form);
        toggleOtherField();
        cleanValidationRules($form);

        return $form.validation('isValid');
    }

    function setupCountrySelects($root, baseUrl) {
        let $billingCountry = $root.find('#b2c-b-country'),
            $shippingCountry = $root.find('#b2c-s-country'),
            $billingRegion = $root.find('#b2c-b-region'),
            $shippingRegion = $root.find('#b2c-s-region');

        loadCountries($billingCountry, baseUrl).done(function () {
            loadRegionsByCountry($billingRegion, $billingCountry.val() || 'US', baseUrl);
        });

        loadCountries($shippingCountry, baseUrl).done(function () {
            loadRegionsByCountry($shippingRegion, $shippingCountry.val() || 'US', baseUrl);
        });

        $billingCountry.off('change.b2cCountry').on('change.b2cCountry', function () {
            loadRegionsByCountry($billingRegion, $(this).val(), baseUrl);
        });

        $shippingCountry.off('change.b2cCountry').on('change.b2cCountry', function () {
            loadRegionsByCountry($shippingRegion, $(this).val(), baseUrl);
        });
    }

    function refreshProjectPdpAddresses(done) {
        req(['FourOver_Project/js/model/project/address-manager', 'uiRegistry'], function (addrMgr, registry) {
            addrMgr.loadProjectAddresses([]).always(function () {
                let project = registry.get('project');

                if (project && typeof project.updateShipmentAddresses === 'function') {
                    project.updateShipmentAddresses();
                }

                done();
            });
        });
    }

    function bindModal($modalRoot) {
        let options = {
            type: 'popup',
            responsive: true,
            modalClass: 'b2c-company-popup',
            title: '',
            buttons: [{
                text: $.mage.__('Save Address & Continue'),
                class: 'action primary b2c-cm-submit',
                click: function () {
                    let self = this,
                        sameAsBilling = $('#b2c-same-as-billing').is(':checked'),
                        cfg = b2cCfg(),
                        businessType = ($('#b2c-modal-business-type').val() || '').trim(),
                        payload;

                    if (!validateModal()) {
                        return;
                    }

                    payload = {
                        form_key: formKey(),
                        company_name: ($('#b2c-modal-company-name').val() || '').trim(),
                        business_type: businessType,
                        same_as_billing: sameAsBilling,
                        billing_address: collectAddress('b'),
                        shipping_address: sameAsBilling ? collectAddress('b') : collectAddress('s')
                    };

                    if (businessType === OTHER_TYPE) {
                        payload.other_business_type = ($('#b2c-modal-other-business-type').val() || '').trim();
                    }

                    $.ajax({
                        url: cfg.profile_save_url,
                        type: 'POST',
                        data: payload,
                        showLoader: true
                    }).done(function () {
                        refreshProjectPdpAddresses(function () {
                            customerData.invalidate(['b2c_onboarding']);
                            customerData.reload(['b2c_onboarding'], true);

                            self.closeModal(true);

                            if (pendingDeferred) {
                                pendingDeferred.resolve();
                            }
                        });
                    }).fail(function (xhr) {
                        let response = xhr.responseJSON;

                        showModalMessage(
                            response && response.message
                                ? response.message
                                : $.mage.__('Unable to save company.')
                        );
                    });
                }
            }]
        };

        modal(options, $modalRoot);

        $modalRoot.off('modalopened.b2cValidation').on('modalopened.b2cValidation', function () {
            let $form = $('#b2c-company-modal-form');

            initFormValidation($form);
            toggleOtherField();
            cleanValidationRules($form);
        });

        modalReady = true;
    }

    function resolveNeedsCompany() {
        let deferred = $.Deferred();

        if (!b2cCfg().is_active) {
            deferred.resolve(false);
            return deferred.promise();
        }

        if (needCheckRequest) {
            return needCheckRequest;
        }

        needCheckRequest = customerData.reload(['b2c_onboarding'], false).then(
            function () {
                return !!b2cPrivateData().needs_company;
            },
            function () {
                return false;
            }
        ).always(function () {
            needCheckRequest = null;
        });

        return needCheckRequest;
    }

    return {
        resolveNeedsCompany: resolveNeedsCompany,

        show: function () {
            let cfg = b2cCfg(),
                baseUrl = cfg.base_url || '/',
                $modalRoot = $('#b2c-company-modal'),
                $form = $('#b2c-company-modal-form');

            pendingDeferred = $.Deferred();

            if (!$modalRoot.length) {
                pendingDeferred.reject();

                return pendingDeferred.promise();
            }

            hideModalMessage();
            populateCustomerName($modalRoot);
            populateBusinessTypes($modalRoot);
            setupCountrySelects($modalRoot, baseUrl);

            $('#b2c-modal-business-type').off('change.b2c').on('change.b2c', function () {
                initFormValidation($form);
                toggleOtherField();
                cleanValidationRules($form);
            });

            toggleOtherField();

            $('#b2c-same-as-billing').off('change.b2c').on('change.b2c', function () {
                let $shippingFieldset = $('#b2c-shipping-fieldset');

                if ($(this).is(':checked')) {
                    $shippingFieldset.addClass('b2c-cm-hidden').hide();
                    return;
                }

                $shippingFieldset.removeClass('b2c-cm-hidden').show();
            });

            if ($('#b2c-same-as-billing').is(':checked')) {
                $('#b2c-shipping-fieldset').addClass('b2c-cm-hidden').hide();
            } else {
                $('#b2c-shipping-fieldset').removeClass('b2c-cm-hidden').show();
            }

            if (!modalReady) {
                bindModal($modalRoot);
            }

            $modalRoot.modal('openModal');

            initFormValidation($form);
            toggleOtherField();
            cleanValidationRules($form);

            return pendingDeferred.promise();
        }
    };
});
