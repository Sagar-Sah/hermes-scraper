/**
 * Created by FindCanary, LLC.
 * Project: 4over
 */

define([
    'uiComponent',
    'FourOver_Project/js/model/project',
    'FourOver_Project/js/model/project-manager',
    'FourOver_Project/js/model/project-messages',
    'FourOver_Project/js/action/get-hash-vars',
    'FourOver_ProjectBookmark/js/action/get-project-bookmark',
    'FourOver_ProjectBookmark/js/action/save-project-bookmark',
    'mage/translate'
], function (Component, project, projectManager, projectMessages, getHashVars, getProjectBookmark, saveProjectBookmark, $t) {
    'use strict';

    var defaultButtonText = $t('Save Project for Later');

    return Component.extend({
        defaults: {
            template: 'FourOver_ProjectBookmark/save-project-bookmark',
            isProjectLoading: false,
            buttonText: defaultButtonText,
            imports: {
                processBookmark: 'project:initialize',
                setDefaultButtonText: 'project-tabs:reset-project'
            },
            exports: {
                resetProject: 'project-tabs:resetProject',
                resetButton: 'project-tabs:resetButton',
                initializeProductOptions: 'project:initializeProductOptions'
            },
            links: {
                isProjectLoading: 'project:isLoading'
            }
        },

        initObservable: function () {
            this._super();
            this.observe([
                'isProjectLoading',
                'resetButton',
                'resetProject',
                'initializeProductOptions',
                'buttonText'
            ]);

            return this;
        },

        isVisible: function () {
            return project().state === 'default' || project().state === 'bookmark';
        },

        setDefaultButtonText: function () {
            this.buttonText(defaultButtonText);
        },

        processBookmark: function () {
            var hashVars = getHashVars();
            if (hashVars.bookmark) {
                this.loadProjectBookmark(hashVars.bookmark);
            } else {
                this.checkAndResetProject();
            }
        },

        checkAndResetProject: function () {
            if (project().state === 'bookmark') {
                this.resetProject.valueHasMutated();
            }
        },

        loadProjectBookmark: function (uuid) {
            this.isProjectLoading(true);

            getProjectBookmark(uuid)
                .done(function (response) {
                        response.state = 'bookmark';
                        projectManager.setProjectData(response);
                        this.initializeProductOptions.valueHasMutated();

                        this.buttonText($t('Update Project for Later'));
                        this.resetButton($t('Cancel Editing'));

                        projectMessages.addSuccessMessage(
                            {
                                message: $t('Project %projectName is loaded.').replace('%projectName', response.name)
                            }
                        );
                    }.bind(this)
                )
                .fail(function () {
                        this.checkAndResetProject();
                    }.bind(this)
                )
                .always(function () {
                        this.isProjectLoading(false);
                    }.bind(this)
                );
        },

        saveAndClose: function () {
            this.saveProjectBookmarkInternal(true);
        },

        saveBookmarkFromArtworkTool: function () {
            this.saveProjectBookmarkInternal(false);
        },

        saveProjectBookmarkInternal: function (resetProjectAfterSave) {
            this.isProjectLoading(true);

            var projectData = projectManager.getProjectData();

            saveProjectBookmark(projectData)
                .done(function () {
                        projectMessages.addSuccessMessage(
                            {
                                message: $t('Project %projectName is saved.').replace('%projectName', projectData.name)
                            }
                        );
                        this.resetProject.valueHasMutated();
                        if (resetProjectAfterSave) {
                            this.resetProject.valueHasMutated();
                        }
                    }.bind(this)
                ).always(function () {
                        this.isProjectLoading(false);
                    }.bind(this)
                );
        }
    });
});
