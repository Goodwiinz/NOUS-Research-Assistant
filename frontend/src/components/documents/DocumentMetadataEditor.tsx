import React, { useState, useEffect, useCallback } from 'react';
import {
  XMarkIcon,
  PencilIcon,
  CheckIcon,
  PlusIcon,
  TrashIcon,
  InformationCircleIcon,
} from '@heroicons/react/24/outline';
import { Document } from '@/types';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';

interface DocumentMetadataEditorProps {
  document: Document | null;
  isOpen: boolean;
  onClose: () => void;
  onSave: (
    documentId: string,
    metadata: DocumentMetadataPayload
  ) => Promise<void>;
}

interface DocumentMetadataPayload {
  title?: string;
  description?: string;
  tags?: string[];
  custom_fields?: Record<string, string | number | boolean>;
}

interface CustomField {
  id: string;
  key: string;
  value: string;
  type: 'text' | 'number' | 'date' | 'boolean';
}

interface MetadataFormData {
  title: string;
  description: string;
  tags: string[];
  customFields: CustomField[];
}

const FIELD_TYPES = [
  { value: 'text', label: 'Text' },
  { value: 'number', label: 'Number' },
  { value: 'date', label: 'Date' },
  { value: 'boolean', label: 'Boolean' },
];

export const DocumentMetadataEditor: React.FC<DocumentMetadataEditorProps> = ({
  document,
  isOpen,
  onClose,
  onSave,
}) => {
  const [formData, setFormData] = useState<MetadataFormData>({
    title: '',
    description: '',
    tags: [],
    customFields: [],
  });
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [hasChanges, setHasChanges] = useState(false);
  const [newTag, setNewTag] = useState('');
  const [newCustomField, setNewCustomField] = useState<CustomField>({
    id: '',
    key: '',
    value: '',
    type: 'text',
  });

  // Initialize form data when document changes
  useEffect(() => {
    if (document) {
      setFormData({
        title: document.title || '',
        description: document.description || '',
        tags: document.tags || [],
        customFields: document.custom_fields
          ? Object.entries(document.custom_fields).map(([key, value]) => ({
              id: Math.random().toString(36).substr(2, 9),
              key,
              value: String(value),
              type:
                typeof value === 'number'
                  ? 'number'
                  : typeof value === 'boolean'
                    ? 'boolean'
                    : 'text',
            }))
          : [],
      });
      setHasChanges(false);
      setSaveError(null);
    }
  }, [document]);

  const handleInputChange = useCallback(
    <K extends keyof MetadataFormData>(
      field: K,
      value: MetadataFormData[K]
    ) => {
      setFormData((prev) => ({ ...prev, [field]: value }));
      setHasChanges(true);
    },
    []
  );

  const handleTitleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      handleInputChange('title', e.target.value);
    },
    [handleInputChange]
  );

  const handleDescriptionChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      handleInputChange('description', e.target.value);
    },
    [handleInputChange]
  );

  const handleAddTag = useCallback(() => {
    if (newTag.trim() && !formData.tags.includes(newTag.trim())) {
      handleInputChange('tags', [...formData.tags, newTag.trim()]);
      setNewTag('');
    }
  }, [newTag, formData.tags, handleInputChange]);

  const handleRemoveTag = useCallback(
    (tagToRemove: string) => {
      handleInputChange(
        'tags',
        formData.tags.filter((tag) => tag !== tagToRemove)
      );
    },
    [formData.tags, handleInputChange]
  );

  const handleAddCustomField = useCallback(() => {
    if (newCustomField.key.trim() && newCustomField.value.trim()) {
      const field: CustomField = {
        ...newCustomField,
        id: Math.random().toString(36).substr(2, 9),
      };
      handleInputChange('customFields', [...formData.customFields, field]);
      setNewCustomField({
        id: '',
        key: '',
        value: '',
        type: 'text',
      });
    }
  }, [newCustomField, formData.customFields, handleInputChange]);

  const handleRemoveCustomField = useCallback(
    (fieldId: string) => {
      handleInputChange(
        'customFields',
        formData.customFields.filter((field) => field.id !== fieldId)
      );
    },
    [formData.customFields, handleInputChange]
  );

  const handleUpdateCustomField = useCallback(
    (fieldId: string, updates: Partial<CustomField>) => {
      setFormData((prev) => ({
        ...prev,
        customFields: prev.customFields.map((field) =>
          field.id === fieldId ? { ...field, ...updates } : field
        ),
      }));
      setHasChanges(true);
    },
    []
  );

  const handleSave = useCallback(async () => {
    if (!document || !hasChanges) return;

    setIsSaving(true);
    setSaveError(null);
    try {
      const customFieldsObject = formData.customFields.reduce(
        (acc, field) => {
          let value: string | number | boolean = field.value;

          // Convert value based on type
          if (field.type === 'number') {
            value = parseFloat(field.value) || 0;
          } else if (field.type === 'boolean') {
            value = field.value.toLowerCase() === 'true';
          } else if (field.type === 'date') {
            value = new Date(field.value).toISOString();
          }

          acc[field.key] = value;
          return acc;
        },
        {} as Record<string, string | number | boolean>
      );

      const updatedMetadata: Partial<Document> = {
        title: formData.title,
        description: formData.description,
        tags: formData.tags,
        custom_fields: customFieldsObject,
      };

      await onSave(document.id, updatedMetadata);
      onClose();
    } catch (error) {
      console.error('Failed to save metadata:', error);
      setSaveError('Failed to save metadata. Please try again.');
    } finally {
      setIsSaving(false);
    }
  }, [document, formData, hasChanges, onSave, onClose]);

  const handleKeyPress = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (e.currentTarget instanceof HTMLInputElement) {
          handleAddTag();
        }
      }
    },
    [handleAddTag]
  );

  if (!document) return null;

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <PencilIcon className="h-5 w-5" />
              <span>Edit Document Metadata</span>
            </div>
            <Button
              onClick={onClose}
              variant="ghost"
              size="sm"
              aria-label="Close dialog"
              className="h-6 w-6 p-0"
            >
              <XMarkIcon className="h-4 w-4" />
            </Button>
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-6 mt-6">
          {/* Basic Information */}
          <div>
            <h3 className="text-lg font-medium text-foreground mb-4">
              Basic Information
            </h3>
            <div className="space-y-4">
              <div>
                <label
                  htmlFor="title"
                  className="block text-sm font-medium text-foreground mb-1"
                >
                  Title
                </label>
                <Input
                  id="title"
                  value={formData.title}
                  onChange={handleTitleChange}
                  placeholder="Document title"
                  className="w-full"
                />
              </div>

              <div>
                <label
                  htmlFor="description"
                  className="block text-sm font-medium text-foreground mb-1"
                >
                  Description
                </label>
                <textarea
                  id="description"
                  value={formData.description}
                  onChange={handleDescriptionChange}
                  placeholder="Document description"
                  rows={3}
                  className="w-full px-3 py-2 border border-border rounded-md focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-primary"
                />
              </div>
            </div>
          </div>

          {/* Tags */}
          <div>
            <h3 className="text-lg font-medium text-foreground mb-4">Tags</h3>
            <div className="space-y-3">
              <div className="flex space-x-2">
                <Input
                  value={newTag}
                  onChange={(e) => setNewTag(e.target.value)}
                  onKeyPress={handleKeyPress}
                  placeholder="Add a tag"
                  className="flex-1"
                />
                <Button onClick={handleAddTag} variant="outline" size="sm">
                  <PlusIcon className="h-4 w-4 mr-1" />
                  Add
                </Button>
              </div>

              {formData.tags.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {formData.tags.map((tag) => (
                    <Badge
                      key={tag}
                      variant="secondary"
                      className="flex items-center space-x-1 px-3 py-1"
                    >
                      <span>{tag}</span>
                      <button
                        onClick={() => handleRemoveTag(tag)}
                        className="ml-1 text-muted-foreground hover:text-foreground"
                        aria-label={`Remove tag: ${tag}`}
                      >
                        <XMarkIcon className="h-3 w-3" />
                      </button>
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Custom Fields */}
          <div>
            <h3 className="text-lg font-medium text-foreground mb-4">
              Custom Fields
            </h3>
            <div className="space-y-4">
              {/* Add new custom field */}
              <div className="border border-dashed border-border rounded-lg p-4">
                <div className="grid grid-cols-12 gap-2">
                  <div className="col-span-4">
                    <Input
                      value={newCustomField.key}
                      onChange={(e) =>
                        setNewCustomField((prev) => ({
                          ...prev,
                          key: e.target.value,
                        }))
                      }
                      placeholder="Field name"
                      className="w-full"
                    />
                  </div>
                  <div className="col-span-5">
                    <Input
                      value={newCustomField.value}
                      onChange={(e) =>
                        setNewCustomField((prev) => ({
                          ...prev,
                          value: e.target.value,
                        }))
                      }
                      placeholder="Value"
                      className="w-full"
                    />
                  </div>
                  <div className="col-span-2">
                    <select
                      value={newCustomField.type}
                      onChange={(e) =>
                        setNewCustomField((prev) => ({
                          ...prev,
                          type: e.target.value as CustomField['type'],
                        }))
                      }
                      className="w-full px-3 py-2 border border-border rounded-md focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-primary text-sm"
                    >
                      {FIELD_TYPES.map((type) => (
                        <option key={type.value} value={type.value}>
                          {type.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="col-span-1">
                    <Button
                      onClick={handleAddCustomField}
                      variant="outline"
                      size="sm"
                      aria-label="Add custom field"
                      className="w-full"
                      disabled={
                        !newCustomField.key.trim() ||
                        !newCustomField.value.trim()
                      }
                    >
                      <PlusIcon className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              </div>

              {/* Existing custom fields */}
              {formData.customFields.length > 0 && (
                <div className="space-y-2">
                  {formData.customFields.map((field) => (
                    <div
                      key={field.id}
                      className="flex items-center space-x-2 p-3 bg-muted rounded-lg"
                    >
                      <div className="flex-1 grid grid-cols-12 gap-2">
                        <div className="col-span-4">
                          <Input
                            value={field.key}
                            onChange={(e) =>
                              handleUpdateCustomField(field.id, {
                                key: e.target.value,
                              })
                            }
                            className="w-full text-sm"
                          />
                        </div>
                        <div className="col-span-5">
                          {field.type === 'boolean' ? (
                            <select
                              value={field.value}
                              onChange={(e) =>
                                handleUpdateCustomField(field.id, {
                                  value: e.target.value,
                                })
                              }
                              className="w-full px-3 py-2 border border-border rounded-md focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-primary text-sm"
                            >
                              <option value="true">True</option>
                              <option value="false">False</option>
                            </select>
                          ) : field.type === 'date' ? (
                            <input
                              type="date"
                              value={field.value}
                              onChange={(e) =>
                                handleUpdateCustomField(field.id, {
                                  value: e.target.value,
                                })
                              }
                              className="w-full px-3 py-2 border border-border rounded-md focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-primary text-sm"
                            />
                          ) : (
                            <Input
                              value={field.value}
                              onChange={(e) =>
                                handleUpdateCustomField(field.id, {
                                  value: e.target.value,
                                })
                              }
                              className="w-full text-sm"
                              type={field.type === 'number' ? 'number' : 'text'}
                            />
                          )}
                        </div>
                        <div className="col-span-2">
                          <select
                            value={field.type}
                            onChange={(e) =>
                              handleUpdateCustomField(field.id, {
                                type: e.target.value as CustomField['type'],
                              })
                            }
                            className="w-full px-3 py-2 border border-border rounded-md focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-primary text-sm"
                          >
                            {FIELD_TYPES.map((type) => (
                              <option key={type.value} value={type.value}>
                                {type.label}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="col-span-1">
                          <Button
                            onClick={() => handleRemoveCustomField(field.id)}
                            variant="outline"
                            size="sm"
                            aria-label="Remove custom field"
                            className="w-full text-destructive hover:text-destructive/80 hover:bg-destructive/10"
                          >
                            <TrashIcon className="h-4 w-4" />
                          </Button>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {saveError && (
            <p role="alert" className="text-sm text-destructive">
              {saveError}
            </p>
          )}

          {/* Actions */}
          <div className="flex justify-end space-x-3 pt-4 border-t">
            <Button onClick={onClose} variant="outline" disabled={isSaving}>
              Cancel
            </Button>
            <Button
              onClick={handleSave}
              disabled={!hasChanges || isSaving}
              className="min-w-[100px]"
            >
              {isSaving ? (
                <div className="flex items-center space-x-2">
                  <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
                  <span>Saving...</span>
                </div>
              ) : (
                <div className="flex items-center space-x-2">
                  <CheckIcon className="h-4 w-4" />
                  <span>Save Changes</span>
                </div>
              )}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default DocumentMetadataEditor;
